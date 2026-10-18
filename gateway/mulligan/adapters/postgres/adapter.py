"""The Postgres adapter: typed tools on prod-db, audit-trigger capture, and conflict-aware undo."""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, ClassVar
from uuid import UUID

import psycopg
from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads
from psycopg_pool import AsyncConnectionPool

from mulligan.adapters.postgres.catalog import (
    TRASH_SCHEMA,
    Table,
    ToolError,
    UndoError,
    fk_edges,
    load_table,
    resolve_table,
    seg,
    trash_aliases,
    untrash,
)
from mulligan.adapters.postgres.tools import (
    check_writable,
    delete_sql,
    insert_sql,
    pg_query,
    soft_drop,
    update_sql,
)
from mulligan.adapters.postgres.undo import names_taken, order_rows, revert_row, toposort, undrop
from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult
from mulligan.models import (
    Action,
    FieldChange,
    FieldChangeOutcome,
    Inverse,
    RewindStep,
    StepResult,
    System,
)
from mulligan.settings import Settings

ABSENT = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # sha256('')
MOD = 2**256
MAX_CHANGES = 200
# Session settings psycopg's loaders need, whatever the server defaults are.
CONN_OPTIONS = "-c datestyle=ISO,YMD -c timezone=UTC -c lc_monetary=C"

WRITE_TOOLS = frozenset({"pg_insert_rows", "pg_update_rows", "pg_delete_rows", "pg_drop_table"})


def jsonable(value: Any) -> Any:
    """Postgres values as plain JSON for agents and the dashboard."""
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes | memoryview):
        return bytes(value).hex()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


async def _configure(conn: AsyncConnection[Any]) -> None:
    # The default jsonb loader turns every JSON number into a float. Keep numerics exact.
    set_json_loads(partial(json.loads, parse_float=Decimal), conn)


async def open_pg_pool(url: str, *, max_size: int = 5) -> AsyncConnectionPool:
    pool: AsyncConnectionPool = AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True, "options": CONN_OPTIONS},
        configure=_configure,
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    return pool


def _plural(n: int, noun: str) -> str:
    if n == 1:
        return f"1 row in {noun}" if not noun.endswith("s") else f"1 {noun[:-1]}"
    return f"{n:,} {noun}"


class PostgresAdapter(Adapter):
    system: ClassVar[System] = System.postgres
    tools: ClassVar[frozenset[str]] = WRITE_TOOLS | {"pg_query"}

    def __init__(
        self, pool: AsyncConnectionPool, ro_pool: AsyncConnectionPool, settings: Settings
    ) -> None:
        self.pool = pool
        self.ro_pool = ro_pool
        self.settings = settings
        self.schemas = settings.pg_allowed_schemas

    @classmethod
    async def open(cls, settings: Settings) -> PostgresAdapter:
        pool = await open_pg_pool(settings.prod_db_url)
        ro_pool = await open_pg_pool(settings.prod_db_readonly_url, max_size=3)
        return cls(pool, ro_pool, settings)

    async def close(self) -> None:
        await self.pool.close()
        await self.ro_pool.close()

    async def reset(self) -> None:
        """Demo only: drop the company's tables (and anything in the trash), clear the audit log,
        reseed from demo/seed/prod.sql and re-attach the audit trigger. Needs the admin URL."""
        if not self.settings.prod_db_admin_url:
            raise RuntimeError("PROD_DB_ADMIN_URL is not set, so prod-db can't be reset")
        seed = (self.settings.seed_dir / "prod.sql").read_text()  # noqa: ASYNC240
        async with await AsyncConnection.connect(
            self.settings.prod_db_admin_url, autocommit=True
        ) as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    SELECT format('%%I.%%I', n.nspname, c.relname) AS t
                      FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p')
                    """,
                    [[*self.schemas, "mulligan_trash"]],
                )
                for row in await cur.fetchall():
                    await conn.execute(
                        sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.SQL(row[0]))
                    )
                await conn.execute("TRUNCATE mulligan_audit.audit_log")
                await conn.execute(seed)
                for schema in self.schemas:
                    await conn.execute("SELECT mulligan_audit.enable_schema(%s)", [schema])

    # --- executing ----------------------------------------------------------------------------

    async def capture_before(self, call: ToolCall) -> Capture:
        # The audit trigger captures old rows inside execute's transaction.
        return Capture(ref=None)

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        args = call.args
        try:
            if call.tool == "pg_query":
                return await self._query(args)
            if call.tool == "pg_drop_table":
                return await self._drop(call, before)
            return await self._write(call, before)
        except ToolError as exc:
            return ToolResult(
                structured_content={"error": str(exc)}, summary=str(exc), is_error=True
            )
        except psycopg.Error as exc:
            # e.g. an FK error naming a table that's in the trash: say the name the agent knows.
            async with self.pool.connection() as conn:
                message = untrash(str(exc), await trash_aliases(conn))
            return ToolResult(structured_content={"error": message}, summary=message, is_error=True)

    async def _query(self, args: dict[str, Any]) -> ToolResult:
        max_rows = min(int(args.get("max_rows") or 200), self.settings.pg_query_max_rows)
        async with self.ro_pool.connection() as ro:
            out = await pg_query(
                ro,
                str(args["sql"]),
                timeout_ms=self.settings.pg_query_timeout_ms,
                max_rows=max_rows,
            )
        out["rows"] = jsonable(out["rows"])
        more = " (truncated)" if out["truncated"] else ""
        return ToolResult(structured_content=out, summary=f"Read {out['row_count']} rows{more}")

    async def _write(self, call: ToolCall, before: Capture) -> ToolResult:
        args = call.args
        op = {"pg_insert_rows": "INSERT", "pg_update_rows": "UPDATE", "pg_delete_rows": "DELETE"}[
            call.tool
        ]
        async with self.pool.connection() as conn:
            t = await resolve_table(conn, str(args["table"]), self.schemas)
            await check_writable(conn, t, op)
            filters = list(args.get("filter") or [])
            all_rows = bool(args.get("all"))
            if op == "INSERT":
                q, params = insert_sql(t, list(args.get("rows") or []))
            elif op == "UPDATE":
                q, params = update_sql(t, filters, dict(args.get("values") or {}), all_rows)
            else:
                q, params = delete_sql(t, filters, all_rows)
            q = sql.Composed([q, sql.SQL(" RETURNING mulligan_audit.canon_row(tgt) AS j")])
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('mulligan.action_id', %s, true)", [call.action_id]
                )
                cur = await conn.execute(q, params)
                touched: list[Any] = await cur.fetchall()
                hashes = await self._after_hashes(conn, call.action_id)
                cur = await conn.execute(
                    "SELECT id, table_oid, schema_name, table_name, op, pk"
                    " FROM mulligan_audit.audit_log WHERE action_id = %s ORDER BY id",
                    [call.action_id],
                )
                audit: list[Any] = await cur.fetchall()
                trashed = any(a["schema_name"] == TRASH_SCHEMA for a in audit)
                aliases = await trash_aliases(conn) if trashed else {}
        direct = {json.dumps([r["j"][c] for c in t.pk], default=str) for r in touched}
        cascaded: Counter[str] = Counter()
        for a in audit:
            key = (
                json.dumps([a["pk"][c] for c in t.pk], default=str)
                if a["table_oid"] == t.oid
                else None
            )
            if key not in direct:
                # A cascade can reach a table in the trash; count it under its old name.
                cascaded[aliases.get(a["table_name"], a["table_name"])] += 1
        before.state.update(
            {
                "after_hashes": hashes,
                "inverse": {
                    "action_id": call.action_id,
                    "audit_ids": [audit[0]["id"], audit[-1]["id"]] if audit else [],
                    "tables": sorted({f"{a['schema_name']}.{a['table_name']}" for a in audit}),
                },
            }
        )
        n = len(touched)
        verb = {"INSERT": "Inserted", "UPDATE": "Updated", "DELETE": "Deleted"}[op]
        prep = {"INSERT": "into", "UPDATE": "in", "DELETE": "from"}[op]
        summary = f"{verb} {_plural(n, 'rows')} {prep} {t.name}"
        if cascaded:
            summary += " (cascaded " + ", ".join(f"{c:,} {tb}" for tb, c in cascaded.items()) + ")"
        if op == "UPDATE":
            summary += f": {', '.join(args.get('values') or {})}"
        content: dict[str, Any] = {
            {"INSERT": "inserted", "UPDATE": "updated", "DELETE": "deleted"}[op]: n,
        }
        if op == "INSERT":
            content["primary_keys"] = jsonable([[r["j"][c] for c in t.pk] for r in touched])
        else:
            content["cascaded"] = dict(cascaded)
        return ToolResult(structured_content=content, summary=summary)

    async def _drop(self, call: ToolCall, before: Capture) -> ToolResult:
        async with self.pool.connection() as conn:
            t = await resolve_table(conn, str(call.args["table"]), self.schemas)
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('mulligan.action_id', %s, true)", [call.action_id]
                )
                row_sum = await self._table_sum(conn, t)
                payload = await soft_drop(conn, t, call.action_id)
                payload["row_hash_sum"] = f"{row_sum:064x}"
        before.state.update({"after_hashes": {f"postgres:{t.path}": ABSENT}, "undrop": payload})
        fks = payload["inbound_fks"]
        summary = f"Dropped table {t.name}"
        if fks:
            verb = "points" if len(fks) == 1 else "point"
            summary += f" ({_plural(len(fks), 'foreign keys')} still {verb} at it)"
        return ToolResult(
            structured_content={
                "dropped": t.qualname,
                "referencing_foreign_keys": [
                    {"table": f["child"], "constraint": f["conname"]} for f in fks
                ],
                "dependent_views": payload["dependent_views"],
            },
            summary=summary,
        )

    async def _after_hashes(self, conn: AsyncConnection[Any], action_id: str) -> dict[str, str]:
        """One hash per (row, column) the action changed, computed in SQL. INSERT: every
        column. UPDATE: changed columns. DELETE: the whole row, as absent. Generated columns
        are skipped. Later audit rows win."""
        cur = await conn.execute(
            """
            SELECT a.table_oid, a.pk, c.col,
                   mulligan_audit.field_hash(
                     CASE WHEN a.op = 'DELETE' THEN NULL ELSE a.new_row -> c.col END) AS h
              FROM mulligan_audit.audit_log a
             CROSS JOIN LATERAL (
                   SELECT unnest(a.changed) WHERE a.op = 'UPDATE'
                   UNION ALL SELECT jsonb_object_keys(a.new_row) WHERE a.op = 'INSERT'
                   UNION ALL SELECT NULL::text WHERE a.op = 'DELETE') AS c(col)
             WHERE a.action_id = %s
             ORDER BY a.id, c.col
            """,
            [action_id],
        )
        rows: list[Any] = await cur.fetchall()
        tables: dict[int, Table] = {}
        hashes: dict[str, str] = {}
        for r in rows:
            if r["table_oid"] not in tables:
                tables[r["table_oid"]] = await load_table(conn, r["table_oid"])
            t = tables[r["table_oid"]]
            if r["col"] is not None and t.columns[r["col"]].generated:
                continue
            key = t.row_key(r["pk"]) + (f"/{seg(r['col'])}" if r["col"] is not None else "")
            hashes[key] = r["h"]
        return hashes

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        if call.tool == "pg_query":
            return Capture()
        if call.tool == "pg_drop_table":
            ref = f"pg-trash:{before.state['undrop']['trash_name']}"
        else:
            ref = f"pg-audit:{call.action_id}"
        return Capture(ref=ref, after_hashes=dict(before.state.get("after_hashes", {})))

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        if call.tool == "pg_query":
            return None
        if call.tool == "pg_drop_table":
            return Inverse(kind="postgres.undrop_table", payload=before.state["undrop"])
        return Inverse(kind="postgres.audit_log", payload=before.state["inverse"])

    # --- undo ---------------------------------------------------------------------------------

    async def plan_revert(self, action: Action) -> RewindStep:
        return await self._revert(action, apply=False)

    async def revert(self, action: Action) -> RewindStep:
        return await self._revert(action, apply=True)

    def _step(
        self, action: Action, changes: list[FieldChange], result: StepResult, detail: str | None
    ) -> RewindStep:
        kept_first = sorted(
            changes, key=lambda c: c.outcome is not FieldChangeOutcome.keep_human_edit
        )
        kept = [
            c.field
            for c in kept_first
            if c.outcome is FieldChangeOutcome.keep_human_edit and c.field in action.after_hashes
        ]
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=result,
            kept_fields=kept,
            changes=kept_first[:MAX_CHANGES],
            changes_truncated=len(kept_first) > MAX_CHANGES,
            detail=detail,
        )

    async def _revert(self, action: Action, *, apply: bool) -> RewindStep:
        if action.inverse is None:
            return self._step(action, [], StepResult.skipped, "Nothing to undo.")
        if action.inverse.kind == "postgres.undrop_table":
            return await self._revert_drop(action, action.inverse.payload, apply=apply)
        async with self.pool.connection() as conn:
            if apply:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('mulligan.action_id', %s, true)", [f"undo_{action.id}"]
                    )
                    return await self._revert_audit(conn, action, apply=True)
            return await self._revert_audit(conn, action, apply=False)

    async def _revert_drop(
        self, action: Action, payload: dict[str, Any], *, apply: bool
    ) -> RewindStep:
        key = next(iter(action.after_hashes), f"postgres:{seg(payload['name'])}")
        async with self.pool.connection() as conn:
            taken = await names_taken(conn, payload)
            change = FieldChange(
                field=key,
                before={"table": f"{payload['schema']}.{payload['name']}"},
                agent_value=None,
                current={"taken": taken} if taken else None,
                outcome=FieldChangeOutcome.keep_human_edit if taken else FieldChangeOutcome.restore,
            )
            if taken:
                detail = f"Name already taken in {payload['schema']}: {', '.join(taken)}."
                return self._step(action, [change], StepResult.kept_human_edit, detail)
            detail = f"Move {payload['name']} back from {payload['trash_schema']}"
            if not apply:
                return self._step(action, [change], StepResult.pending, detail)
            async with conn.transaction():
                await undrop(conn, payload)
        return self._step(action, [change], StepResult.restored, detail.replace("Move", "Moved"))

    async def _revert_audit(
        self, conn: AsyncConnection[Any], action: Action, *, apply: bool
    ) -> RewindStep:
        cur = await conn.execute(
            "SELECT * FROM mulligan_audit.audit_log WHERE action_id = %s ORDER BY id", [action.id]
        )
        audit: list[dict[str, Any]] = list(await cur.fetchall())
        if not audit:
            return self._step(action, [], StepResult.skipped, "The action changed no rows.")
        oids = list(dict.fromkeys(a["table_oid"] for a in audit))
        tables = {o: await load_table(conn, o) for o in oids}
        current = await self._current(conn, audit, tables)

        changes: list[FieldChange] = []
        restore_cols: dict[int, list[str]] = {}  # audit id -> columns to put back (UPDATE)
        revert_ids: set[int] = set()  # audit ids to invert (INSERT / DELETE)
        for a in audit:
            t = tables[a["table_oid"]]
            cur_row, cur_hashes = current[a["id"]]
            row_key = t.row_key(a["pk"])
            if a["op"] == "DELETE":
                ok = cur_row is None
                changes.append(
                    FieldChange(
                        field=row_key,
                        before=_display(a["old_row"]),
                        agent_value=None,
                        current=_display(cur_row),
                        outcome=_outcome(ok),
                    )
                )
                if ok:
                    revert_ids.add(a["id"])
                continue
            cols = (
                [c for c in a["changed"] or [] if not t.columns[c].generated]
                if a["op"] == "UPDATE"
                else [c for c in a["new_row"] if c in t.columns and not t.columns[c].generated]
            )
            edited = []
            for col in cols:
                key = f"{row_key}/{seg(col)}"
                ok = cur_row is not None and cur_hashes.get(col) == action.after_hashes.get(key)
                if a["op"] == "UPDATE" and ok:
                    restore_cols.setdefault(a["id"], []).append(col)
                if not ok and cur_row is not None:
                    edited.append(col)
                if a["op"] == "UPDATE" or not ok:
                    changes.append(
                        FieldChange(
                            field=key,
                            before=_display((a["old_row"] or {}).get(col)),
                            agent_value=_display(a["new_row"].get(col)),
                            current=_display(cur_row.get(col) if cur_row else None),
                            outcome=_outcome(ok or cur_row is None and a["op"] == "INSERT"),
                        )
                    )
            if a["op"] == "INSERT":
                if cur_row is not None and not edited:
                    revert_ids.add(a["id"])
                elif edited:
                    # A human changed a row the agent inserted: keep the whole row.
                    changes.append(
                        FieldChange(
                            field=row_key,
                            before=None,
                            agent_value=_display(a["new_row"]),
                            current=_display(cur_row),
                            outcome=FieldChangeOutcome.keep_human_edit,
                        )
                    )
                else:
                    changes.append(
                        FieldChange(
                            field=row_key,
                            before=None,
                            agent_value=_display(a["new_row"]),
                            current=None,
                            outcome=FieldChangeOutcome.restore,
                        )
                    )

        kept = any(c.outcome is FieldChangeOutcome.keep_human_edit for c in changes)
        in_trash = any(t.schema == TRASH_SCHEMA for t in tables.values())
        aliases = await trash_aliases(conn) if in_trash else {}
        detail = _describe(audit, tables, revert_ids, restore_cols, aliases)
        if not apply:
            return self._step(action, changes, StepResult.pending, detail)
        await self._apply(conn, audit, tables, revert_ids, restore_cols)
        result = StepResult.kept_human_edit if kept else StepResult.restored
        return self._step(action, changes, result, detail)

    async def _current(
        self, conn: AsyncConnection[Any], audit: list[dict[str, Any]], tables: dict[int, Table]
    ) -> dict[int, tuple[dict[str, Any] | None, dict[str, str]]]:
        """For each audit row: the live row (canonical) and the hash of each of its columns."""
        out: dict[int, tuple[dict[str, Any] | None, dict[str, str]]] = {}
        by_table: dict[int, list[int]] = defaultdict(list)
        for a in audit:
            by_table[a["table_oid"]].append(a["id"])
        for oid, ids in by_table.items():
            t = tables[oid]
            q = sql.SQL(
                """
                SELECT a.id, cur.j,
                       (SELECT jsonb_object_agg(k, mulligan_audit.field_hash(cur.j -> k))
                          FROM jsonb_object_keys(cur.j) AS k) AS hashes
                  FROM mulligan_audit.audit_log a
                  LEFT JOIN LATERAL (
                       SELECT mulligan_audit.canon_row(tgt) AS j FROM {t} AS tgt
                        CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.pk) AS w
                        WHERE {m}) AS cur ON true
                 WHERE a.id = ANY(%s)
                """
            ).format(t=t.ident, m=t.pk_match("tgt", "w"))
            cur = await conn.execute(q, [ids])
            for r in await cur.fetchall():
                row: Any = r
                out[row["id"]] = (row["j"], dict(row["hashes"] or {}))
        return out

    async def _apply(
        self,
        conn: AsyncConnection[Any],
        audit: list[dict[str, Any]],
        tables: dict[int, Table],
        revert_ids: set[int],
        restore_cols: dict[int, list[str]],
    ) -> None:
        """(1) delete rows it inserted, children first; (2) re-insert rows it deleted, parents
        first by FK dependency; (3) put back updated columns, parents first, newest first."""
        oids = list(tables)
        parents: dict[int, set[int]] = defaultdict(set)
        self_fks: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for e in await fk_edges(conn, oids):
            if e["child"] == e["parent"]:
                self_fks[e["child"]].append(e)
            else:
                parents[e["child"]].add(e["parent"])
        order = toposort(oids, parents)
        by: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
        for a in audit:
            by[(a["table_oid"], a["op"])].append(a)
        for oid in reversed(order):
            for a in reversed(order_rows(by[(oid, "INSERT")], self_fks[oid], "new_row")):
                if a["id"] in revert_ids:
                    await revert_row(conn, tables[oid], a)
        for oid in order:
            for a in order_rows(by[(oid, "DELETE")], self_fks[oid], "old_row"):
                if a["id"] in revert_ids:
                    await revert_row(conn, tables[oid], a)
        for oid in order:
            for a in reversed(by[(oid, "UPDATE")]):
                if restore_cols.get(a["id"]):
                    await revert_row(conn, tables[oid], a, restore_cols[a["id"]])

    # --- checksums ----------------------------------------------------------------------------

    async def _tables(self, conn: AsyncConnection[Any]) -> list[Table]:
        cur = await conn.execute(
            """
            SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p') AND NOT c.relispartition
             ORDER BY n.nspname, c.relname
            """,
            [list(self.schemas)],
        )
        return [await load_table(conn, r["oid"]) for r in await cur.fetchall()]

    async def checksum(self) -> str | None:
        """Sum (mod 2^256) of sha256 over each row's table path and canonical form. A sum, not a
        sorted hash, so a kept human edit can be accounted for by swapping one row's term."""
        total = 0
        async with self.pool.connection() as conn:
            for t in await self._tables(conn):
                total = (total + await self._table_sum(conn, t)) % MOD
        return f"{total:064x}"

    async def _table_sum(
        self, conn: AsyncConnection[Any], t: Table, path: str | None = None
    ) -> int:
        q = sql.SQL(
            "SELECT mulligan_audit.field_hash(jsonb_build_array(%s::text,"
            " mulligan_audit.canon_row(tgt))) AS h FROM {t} AS tgt"
        ).format(t=t.ident)
        cur = await conn.execute(q, [path or t.path])
        rows: list[Any] = await cur.fetchall()
        return sum(int(r["h"], 16) for r in rows) % MOD

    async def verify_scope(
        self, actions: list[Action], steps: list[RewindStep]
    ) -> tuple[str, str] | None:
        """(expected, actual) over exactly what the rewound actions touched, so edits people made
        elsewhere don't count against the rewind. Every row in the actions' audit log should be
        back in its pre-session form (with kept human edits on top), and every table they dropped
        should hold the rows it had when it was dropped."""
        ids = [a.id for a in actions]
        kept = _kept_rows(steps)
        expected = actual = 0
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM mulligan_audit.audit_log WHERE action_id = ANY(%s) ORDER BY id",
                [ids],
            )
            audit: list[Any] = await cur.fetchall()
            tables: dict[int, Table] = {}
            # Row key -> the audit rows that touched it, oldest first, each with the image that
            # holds the key: 'pk', or 'old_row' where an UPDATE moved the row off this key.
            touches: dict[str, list[tuple[Any, str]]] = defaultdict(list)
            changed: dict[str, set[str]] = defaultdict(set)
            for a in audit:
                if a["table_oid"] not in tables:
                    tables[a["table_oid"]] = await load_table(conn, a["table_oid"])
                t = tables[a["table_oid"]]
                key = t.row_key(a["pk"])
                touches[key].append((a, "pk"))
                changed[key].update(a["changed"] or [])
                if _moves_pk(t, a):
                    old_key = t.row_key({c: a["old_row"][c] for c in t.pk})
                    touches[old_key].append((a, "old_row"))
                    changed[old_key].update(a["changed"])
            for row_key, entries in touches.items():
                a, by = entries[0]
                t = tables[a["table_oid"]]
                cols = kept.get(row_key)
                arrived = by == "pk" and (a["op"] == "INSERT" or _moves_pk(t, a))
                if not arrived and a["op"] == "UPDATE":
                    # Columns the session changed go back to their old values unless a human
                    # kept them; columns it never touched are whatever people left there.
                    # Generated columns follow from the others, so they're left as they are.
                    restore = {
                        c
                        for c in changed[row_key] - (cols or set())
                        if c in t.columns and not t.columns[c].generated
                    }
                    h = await self._row_hashes(conn, t, a, restore, by)
                    actual += h["cur"]
                    if row_key in kept and cols is None:
                        expected += h["cur"]
                    elif not h["gone"]:
                        expected += h["upd"]
                    elif by == "old_row":
                        expected += h["pre"]  # the row should be back on its old key
                    continue
                h = await self._row_hashes(conn, t, a, set(), by)
                actual += h["cur"]
                if row_key in kept:
                    expected += h["cur"]
                elif not arrived:
                    expected += h["pre"]
            created = {a.id: a.created_at for a in actions}
            for action in actions:
                if action.inverse is None or action.inverse.kind != "postgres.undrop_table":
                    continue
                p = action.inverse.payload
                if "row_hash_sum" not in p:
                    return None
                path = seg(p["name"] if p["schema"] == "public" else f"{p['schema']}.{p['name']}")
                expected += int(p["row_hash_sum"], 16)
                # Rows of this table the session also wrote are already counted above: take
                # their drop-time form out of the expected side and their current form out of
                # the actual side.
                for entries in touches.values():
                    a, by = entries[0]
                    if a["table_oid"] != p["table_oid"]:
                        continue
                    form = _drop_time_form(
                        tables[a["table_oid"]], entries, created, action.created_at
                    )
                    if form is not None:
                        expected -= await self._audit_hash(conn, path, *form)
                    h = await self._row_hashes(conn, tables[a["table_oid"]], a, None, by)
                    actual -= h["cur"]
                try:
                    t = await load_table(conn, p["table_oid"])
                except UndoError:
                    continue
                if t.schema == p["schema"]:
                    actual += await self._table_sum(conn, t, path)
        return f"{expected % MOD:064x}", f"{actual % MOD:064x}"

    async def _audit_hash(
        self, conn: AsyncConnection[Any], path: str, audit_id: int, image: str
    ) -> int:
        """Row hash of one audit row's 'old_row' or 'new_row'."""
        cur = await conn.execute(
            sql.SQL(
                "SELECT mulligan_audit.field_hash(jsonb_build_array(%s::text, {})) AS h"
                " FROM mulligan_audit.audit_log WHERE id = %s"
            ).format(sql.Identifier(image)),
            [path, audit_id],
        )
        row: Any = await cur.fetchone()
        return int(row["h"], 16)

    async def _row_hashes(
        self,
        conn: AsyncConnection[Any],
        t: Table,
        a: dict[str, Any],
        cols: set[str] | None,
        by: str = "pk",
    ) -> dict[str, Any]:
        """pre: the audit row's old form. cur: the live row (0 if gone), looked up by the key in
        `by` ('pk', or 'old_row' for the key an UPDATE moved the row off). upd: the live row
        with `cols` put back to their old values."""
        q = sql.SQL(
            """
            SELECT mulligan_audit.field_hash(jsonb_build_array(%(p)s::text, a.old_row)) AS pre,
                   mulligan_audit.field_hash(jsonb_build_array(%(p)s::text, cur.j)) AS cur,
                   mulligan_audit.field_hash(jsonb_build_array(%(p)s::text,
                     cur.j || coalesce((SELECT jsonb_object_agg(k, a.old_row -> k)
                                          FROM unnest(%(cols)s::text[]) AS k), '{{}}'))) AS upd,
                   cur.j IS NULL AS gone
              FROM mulligan_audit.audit_log a
              LEFT JOIN LATERAL (
                   SELECT mulligan_audit.canon_row(tgt) AS j FROM {t} AS tgt
                    CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, {by}) AS w
                    WHERE {m}) AS cur ON true
             WHERE a.id = %(id)s
            """
        ).format(t=t.ident, m=t.pk_match("tgt", "w"), by=sql.Identifier("a", by))
        cur = await conn.execute(q, {"p": t.path, "cols": sorted(cols or []), "id": a["id"]})
        h: Any = await cur.fetchone()
        return {
            "pre": int(h["pre"], 16) if a["op"] != "INSERT" else 0,
            "cur": 0 if h["gone"] else int(h["cur"], 16),
            "upd": int(h["upd"], 16),
            "gone": h["gone"],
        }


def _moves_pk(t: Table, a: dict[str, Any]) -> bool:
    """An UPDATE that changed a primary-key column: the row moved to a new key."""
    return a["op"] == "UPDATE" and bool(set(a["changed"] or []) & set(t.pk))


def _drop_time_form(
    t: Table, entries: list[tuple[Any, str]], created: dict[str, datetime], dropped_at: datetime
) -> tuple[int, str] | None:
    """(audit id, image) holding the row at this key when its table was dropped, or None if
    no row had the key then. Writes after the drop (cascades into the trashed table) don't
    count, so a row first written there is still in its drop-time form: the first old_row."""
    first, by = entries[0]
    arrived = by == "pk" and (first["op"] == "INSERT" or _moves_pk(t, first))
    form: tuple[int, str] | None = None if arrived else (first["id"], "old_row")
    for a, by in entries:
        if created.get(a["action_id"], dropped_at) < dropped_at:
            gone = by == "old_row" or a["op"] == "DELETE"
            form = None if gone else (a["id"], "new_row")
    return form


def _kept_rows(steps: list[RewindStep]) -> dict[str, set[str] | None]:
    """Row key -> the columns a human kept (None: the whole row)."""
    kept: dict[str, set[str] | None] = {}
    for step in steps:
        for change in step.changes:
            if change.outcome is not FieldChangeOutcome.keep_human_edit:
                continue
            parts = change.field.split("/")
            if len(parts) == 2:
                kept[change.field] = None
            elif len(parts) == 3:
                cols = kept.setdefault("/".join(parts[:2]), set())
                if cols is not None:
                    cols.add(parts[2])
    return kept


def _outcome(ok: bool) -> FieldChangeOutcome:
    return FieldChangeOutcome.restore if ok else FieldChangeOutcome.keep_human_edit


def _display(value: Any) -> Any:
    return jsonable(value)


def _describe(
    audit: list[dict[str, Any]],
    tables: dict[int, Table],
    revert_ids: set[int],
    restore_cols: dict[int, list[str]],
    aliases: dict[str, str],
) -> str:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for a in audit:
        name = tables[a["table_oid"]].name
        name = aliases.get(name, name)
        if a["id"] in revert_ids:
            counts["Re-insert" if a["op"] == "DELETE" else "Delete"][name] += 1
        elif restore_cols.get(a["id"]):
            counts["Restore"][name] += 1
    parts = [
        f"{verb} " + ", ".join(_plural(n, t) if n == 1 else f"{n:,} {t}" for t, n in c.items())
        for verb, c in counts.items()
    ]
    if not parts:
        return "Nothing to restore."
    text = "; ".join(parts)
    return text + (" (parents first)" if "Re-insert" in counts else "")


__all__ = ["PostgresAdapter", "ToolError", "UndoError", "open_pg_pool"]
