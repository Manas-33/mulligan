"""The action log in mulligan-db: sessions, actions, held calls, rewind jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from mulligan.models import Action, ActionStatus, RewindJob, Session, System


def _doc(model: Action | Session | RewindJob) -> Jsonb:
    return Jsonb(model.model_dump(mode="json"))


class LogStore:
    """Implements `interfaces.ActionStore` plus what the core and the REST API need."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    # --- sessions ---------------------------------------------------------------------------

    async def save_session(self, session: Session, mcp_session_id: str | None = None) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO sessions (id, mcp_session_id, agent_id, status, started_at, doc)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, doc = EXCLUDED.doc
                """,
                (
                    session.id,
                    mcp_session_id,
                    session.agent.id,
                    session.status.value,
                    session.started_at,
                    _doc(session),
                ),
            )

    async def get_session(self, session_id: str) -> Session | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT doc FROM sessions WHERE id = %s", (session_id,))
            row: Any = await cur.fetchone()
        return Session.model_validate(row["doc"]) if row else None

    async def session_for_mcp(self, mcp_session_id: str) -> Session | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT doc FROM sessions WHERE mcp_session_id = %s", (mcp_session_id,)
            )
            row: Any = await cur.fetchone()
        return Session.model_validate(row["doc"]) if row else None

    async def list_sessions(
        self, *, status: str | None = None, agent_kind: str | None = None, limit: int = 200
    ) -> list[Session]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT doc FROM sessions
                WHERE (%(status)s::text IS NULL OR status = %(status)s)
                  AND (%(kind)s::text IS NULL OR doc->'agent'->>'kind' = %(kind)s)
                ORDER BY started_at DESC LIMIT %(limit)s
                """,
                {"status": status, "kind": agent_kind, "limit": limit},
            )
            rows: list[Any] = await cur.fetchall()
        return [Session.model_validate(r["doc"]) for r in rows]

    # --- actions ----------------------------------------------------------------------------

    async def save(self, action: Action) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO actions (id, session_id, system, status, created_at, doc)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (id, created_at)
                DO UPDATE SET status = EXCLUDED.status, doc = EXCLUDED.doc
                """,
                (
                    action.id,
                    action.session_id,
                    action.system.value,
                    action.status.value,
                    action.created_at,
                    _doc(action),
                ),
            )

    async def get(self, action_id: str) -> Action | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT doc FROM actions WHERE id = %s", (action_id,))
            row: Any = await cur.fetchone()
        return Action.model_validate(row["doc"]) if row else None

    async def list_actions(
        self,
        *,
        session_id: str | None = None,
        since: datetime | None = None,
        system: System | str | None = None,
        status: ActionStatus | str | None = None,
        ids: list[str] | None = None,
        newest_first: bool = False,
        limit: int | None = 200,
    ) -> list[Action]:
        order = "DESC" if newest_first else "ASC"
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT doc FROM actions
                WHERE (%(session_id)s::text IS NULL OR session_id = %(session_id)s)
                  AND (%(since)s::timestamptz IS NULL OR created_at >= %(since)s)
                  AND (%(system)s::text IS NULL OR system = %(system)s)
                  AND (%(status)s::text IS NULL OR status = %(status)s)
                  AND (%(ids)s::text[] IS NULL OR id = ANY(%(ids)s))
                ORDER BY created_at {order}, id {order}
                LIMIT %(limit)s
                """,  # noqa: S608 (order is one of two literals)
                {
                    "session_id": session_id,
                    "since": since,
                    "system": getattr(system, "value", system),
                    "status": getattr(status, "value", status),
                    "ids": ids,
                    "limit": limit,
                },
            )
            rows: list[Any] = await cur.fetchall()
        return [Action.model_validate(r["doc"]) for r in rows]

    async def list_by_status(self, *statuses: str) -> list[Action]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT doc FROM actions WHERE status = ANY(%s) ORDER BY created_at",
                ([getattr(s, "value", s) for s in statuses],),
            )
            rows: list[Any] = await cur.fetchall()
        return [Action.model_validate(r["doc"]) for r in rows]

    async def count_actions(self, session_id: str) -> int:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM actions WHERE session_id = %s", (session_id,)
            )
            row: Any = await cur.fetchone()
        return int(row["n"])

    # --- held calls (unredacted args) -------------------------------------------------------

    async def put_held_call(self, action_id: str, tool: str, args: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO held_calls (action_id, tool, args) VALUES (%s, %s, %s)"
                " ON CONFLICT (action_id) DO NOTHING",
                (action_id, tool, Jsonb(args)),
            )

    async def take_held_call(self, action_id: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM held_calls WHERE action_id = %s RETURNING args", (action_id,)
            )
            row: Any = await cur.fetchone()
        return dict(row["args"]) if row else None

    async def drop_held_call(self, action_id: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM held_calls WHERE action_id = %s", (action_id,))

    # --- per-action checksums ---------------------------------------------------------------

    async def put_checksum(
        self, action_id: str, system: System, pre: str, post: str | None
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO action_checksums (action_id, system, pre, post)"
                " VALUES (%s, %s, %s, %s)"
                " ON CONFLICT (action_id) DO UPDATE SET pre = EXCLUDED.pre, post = EXCLUDED.post",
                (action_id, system.value, pre, post),
            )

    async def pre_checksums(self, action_ids: list[str]) -> dict[str, str]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT action_id, pre FROM action_checksums WHERE action_id = ANY(%s)",
                (action_ids,),
            )
            rows: list[Any] = await cur.fetchall()
        return {r["action_id"]: r["pre"] for r in rows}

    # --- rewind jobs ------------------------------------------------------------------------

    async def save_job(self, job: RewindJob) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO rewind_jobs (id, status, created_at, doc) VALUES (%s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, doc = EXCLUDED.doc
                """,
                (job.id, job.status.value, job.created_at, _doc(job)),
            )

    async def get_job(self, job_id: str) -> RewindJob | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT doc FROM rewind_jobs WHERE id = %s", (job_id,))
            row: Any = await cur.fetchone()
        return RewindJob.model_validate(row["doc"]) if row else None

    async def list_jobs(
        self, *, session_id: str | None = None, limit: int = 200
    ) -> list[RewindJob]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT doc FROM rewind_jobs
                WHERE (%(sid)s::text IS NULL OR doc->'scope'->>'session_id' = %(sid)s)
                ORDER BY created_at DESC LIMIT %(limit)s
                """,
                {"sid": session_id, "limit": limit},
            )
            rows: list[Any] = await cur.fetchall()
        return [RewindJob.model_validate(r["doc"]) for r in rows]

    # --- demo -------------------------------------------------------------------------------

    async def clear(self) -> None:
        """Wipe the log (demo reset only)."""
        async with self.pool.connection() as conn:
            await conn.execute(
                "TRUNCATE sessions, actions, held_calls, action_checksums, rewind_jobs, events"
                " RESTART IDENTITY"
            )
