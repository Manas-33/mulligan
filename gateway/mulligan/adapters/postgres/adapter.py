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

from psycopg import AsyncConnection, sql
from psycopg.rows import dict_row
from psycopg.types.json import set_json_loads
from psycopg_pool import AsyncConnectionPool

from mulligan.adapters.postgres.catalog import (
    Table,
    ToolError,
    UndoError,
    fk_edges,
    load_table,
    resolve_table,
    seg,
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
        direct = {json.dumps([r["j"][c] for c in t.pk], default=str) for r in touched}
        cascaded: Counter[str] = Counter()
        for a in audit:
            key = (
                json.dumps([a["pk"][c] for c in t.pk], default=str)
                if a["table_oid"] == t.oid
                else None
            )
            if key not in direct:
                cascaded[a["table_name"]] += 1
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
                payload = await soft_drop(conn, t, call.action_id)
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
        detail = _describe(audit, tables, revert_ids, restore_cols)
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
                q = sql.SQL(
                    "SELECT mulligan_audit.field_hash(jsonb_build_array(%s::text,"
                    " mulligan_audit.canon_row(tgt))) AS h FROM {t} AS tgt"
                ).format(t=t.ident)
                cur = await conn.execute(q, [t.path])
                rows: list[Any] = await cur.fetchall()
                for r in rows:
                    total = (total + int(r["h"], 16)) % MOD
        return f"{total:064x}"

    async def expected_checksum(self, pre: str, steps: list[RewindStep]) -> str:
        """`pre` with every row a human kept swapped in: for a row that existed before the
        session, its pre-session form with the kept columns (or, if a human reused its key,
        the whole current row); for a row the agent inserted and a human edited, the row."""
        kept_rows: dict[str, set[str] | None] = {}  # row key -> kept columns (None: whole row)
        for step in steps:
            for change in step.changes:
                if change.outcome is not FieldChangeOutcome.keep_human_edit:
                    continue
                parts = change.field.split("/")
                if len(parts) == 2:
                    kept_rows[change.field] = None
                elif len(parts) == 3:
                    row_key = "/".join(parts[:2])
                    cols = kept_rows.setdefault(row_key, set())
                    if cols is not None:
                        cols.add(parts[2])
        if not kept_rows:
            return pre
        total = int(pre, 16)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM mulligan_audit.audit_log WHERE action_id = ANY(%s) ORDER BY id",
                [[s.action_id for s in steps]],
            )
            audit: list[Any] = await cur.fetchall()
            tables: dict[int, Table] = {}
            first: dict[str, Any] = {}
            for a in audit:
                if a["table_oid"] not in tables:
                    tables[a["table_oid"]] = await load_table(conn, a["table_oid"])
                first.setdefault(tables[a["table_oid"]].row_key(a["pk"]), a)
            for row_key, cols in kept_rows.items():
                a = first.get(row_key)
                if a is None:
                    continue
                t = tables[a["table_oid"]]
                q = sql.SQL(
                    """
                    SELECT mulligan_audit.field_hash(jsonb_build_array(%(p)s::text, a.old_row)) AS pre,
                           mulligan_audit.field_hash(jsonb_build_array(%(p)s::text, cur.j)) AS cur,
                           mulligan_audit.field_hash(jsonb_build_array(%(p)s::text,
                             a.old_row || coalesce((SELECT jsonb_object_agg(k, cur.j -> k)
                                                      FROM unnest(%(cols)s::text[]) AS k), '{{}}'))) AS mix,
                           cur.j IS NULL AS gone
                      FROM mulligan_audit.audit_log a
                      LEFT JOIN LATERAL (
                           SELECT mulligan_audit.canon_row(tgt) AS j FROM {t} AS tgt
                            CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.pk) AS w
                            WHERE {m}) AS cur ON true
                     WHERE a.id = %(id)s
                    """
                ).format(t=t.ident, m=t.pk_match("tgt", "w"))
                cur = await conn.execute(
                    q, {"p": t.path, "cols": sorted(cols or []), "id": a["id"]}
                )
                h: Any = await cur.fetchone()
                existed = a["op"] != "INSERT"
                minus = int(h["pre"], 16) if existed else 0
                if h["gone"]:
                    plus = 0
                elif cols is None or not existed:
                    plus = int(h["cur"], 16)
                else:
                    plus = int(h["mix"], 16)
                total = (total - minus + plus) % MOD
        return f"{total:064x}"


def _outcome(ok: bool) -> FieldChangeOutcome:
    return FieldChangeOutcome.restore if ok else FieldChangeOutcome.keep_human_edit


def _display(value: Any) -> Any:
    return jsonable(value)


def _describe(
    audit: list[dict[str, Any]],
    tables: dict[int, Table],
    revert_ids: set[int],
    restore_cols: dict[int, list[str]],
) -> str:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for a in audit:
        name = tables[a["table_oid"]].name
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
