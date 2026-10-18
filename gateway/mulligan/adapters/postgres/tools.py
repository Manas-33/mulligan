"""The typed Postgres tools. Agent input never becomes SQL text: names are checked against the
catalog and quoted, and values go in as one JSON parameter that Postgres converts to the column
types with jsonb_populate_record (the same conversion undo uses to put old rows back)."""

from __future__ import annotations

import json
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from mulligan.adapters.postgres.catalog import (
    TRASH_NOTE,
    TRASH_SCHEMA,
    Table,
    ToolError,
    fit_ident,
    unaudited_tables,
)

_CMP = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}


def _as_col(t: Table, col: str, json_expr: sql.Composable) -> sql.Composable:
    """A JSON value converted to the column's type by Postgres itself."""
    return sql.SQL("(jsonb_populate_record(NULL::{t}, jsonb_build_object({c}, {v}))).{ci}").format(
        t=t.ident, c=sql.Literal(col), v=json_expr, ci=sql.Identifier(col)
    )


def build_where(
    t: Table, filters: list[dict[str, Any]], all_rows: bool
) -> tuple[sql.Composable, list[Any]]:
    if not filters:
        if not all_rows:
            raise ToolError("filter is empty; pass all=true to touch every row")
        return sql.SQL("TRUE"), []
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for f in filters:
        col, op = f.get("column"), str(f.get("op", "")).strip().lower()
        if not isinstance(col, str) or col not in t.columns:
            raise ToolError(f"unknown column {col!r} on {t.qualname}")
        c = sql.Identifier("tgt", col)
        if op in _CMP:
            if f.get("value") is None:
                raise ToolError(f"{op} needs a value; use is_null for NULL")
            rhs = _as_col(t, col, sql.SQL("%s::jsonb"))
            parts.append(sql.SQL("{} {} {}").format(c, sql.SQL(_CMP[op]), rhs))
            params.append(Jsonb(f["value"]))
        elif op in ("in", "not_in"):
            if not isinstance(f.get("value"), list):
                raise ToolError(f"{op} needs a list value")
            arr = sql.SQL("ARRAY(SELECT {} FROM jsonb_array_elements(%s::jsonb) AS e)").format(
                _as_col(t, col, sql.SQL("e"))
            )
            tmpl = "{} = ANY({})" if op == "in" else "{} <> ALL({})"
            parts.append(sql.SQL(tmpl).format(c, arr))
            params.append(Jsonb(f["value"]))
        elif op in ("like", "ilike"):
            parts.append(sql.SQL("{}::text {} %s::text").format(c, sql.SQL(op.upper())))
            params.append(str(f.get("value")))
        elif op in ("is_null", "is_not_null"):
            parts.append(sql.SQL("{} {}").format(c, sql.SQL(op.upper().replace("_", " "))))
        else:
            raise ToolError(f"unknown op {op!r}")
    return sql.SQL(" AND ").join(parts), params


async def check_writable(conn: AsyncConnection[Any], t: Table, op: str) -> None:
    if not t.pk:
        raise ToolError(f"{t.qualname} has no primary key, so writes to it can't be undone")
    missing = await unaudited_tables(conn, t, op)
    if missing:
        raise ToolError(f"refusing {op}: no audit trigger on {', '.join(missing)}")


def insert_sql(t: Table, rows: list[dict[str, Any]]) -> tuple[sql.Composable, list[Any]]:
    if not rows:
        raise ToolError("rows is empty")
    cols = list(rows[0])
    if any(list(r) != cols for r in rows):
        raise ToolError("every row needs the same columns in the same order")
    unknown = set(cols) - set(t.columns)
    if unknown:
        raise ToolError(f"unknown columns on {t.qualname}: {', '.join(sorted(unknown))}")
    blocked = [c for c in cols if t.columns[c].generated or t.columns[c].identity == "a"]
    if blocked:
        raise ToolError(f"generated columns can't be set: {', '.join(blocked)}")
    ci = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL(
        "INSERT INTO {t} AS tgt ({c}) SELECT {c} FROM jsonb_populate_recordset(NULL::{t}, %s::jsonb)"
    ).format(t=t.ident, c=ci)
    return q, [Jsonb(rows)]


def update_sql(
    t: Table, filters: list[dict[str, Any]], values: dict[str, Any], all_rows: bool
) -> tuple[sql.Composable, list[Any]]:
    if not values:
        raise ToolError("values is empty")
    bad = [c for c in values if c not in t.writable or t.columns[c].identity == "a"]
    if bad:
        raise ToolError(f"can't set {', '.join(bad)} on {t.qualname}")
    where, wparams = build_where(t, filters, all_rows)
    sets = sql.SQL(", ").join(sql.SQL("{c} = _v.{c}").format(c=sql.Identifier(c)) for c in values)
    q = sql.SQL(
        "UPDATE {t} AS tgt SET {s} FROM jsonb_populate_record(NULL::{t}, %s::jsonb) AS _v WHERE {w}"
    ).format(t=t.ident, s=sets, w=where)
    return q, [Jsonb(values), *wparams]


def delete_sql(
    t: Table, filters: list[dict[str, Any]], all_rows: bool
) -> tuple[sql.Composable, list[Any]]:
    where, wparams = build_where(t, filters, all_rows)
    return sql.SQL("DELETE FROM {t} AS tgt WHERE {w}").format(t=t.ident, w=where), wparams


def trash_names(action_id: str, table: str) -> tuple[str, str]:
    """(table name in trash, prefix for its indexes and sequences). The action id goes first so
    truncation never cuts the unique part. Lowercase so it works unquoted."""
    prefix = action_id.lower()
    return fit_ident(f"{prefix}__{table}"), prefix


async def soft_drop(conn: AsyncConnection[Any], t: Table, action_id: str) -> dict[str, Any]:
    """Rename the table, its indexes and owned sequences to action-prefixed names, revoke the
    read-only role's SELECT on dependent views, then move the table into mulligan_trash.
    Runs inside the caller's transaction. Returns the undrop payload."""
    trash_table, prefix = trash_names(action_id, t.name)
    cur = await conn.execute(
        """
        SELECT c.relname AS name, c.relkind::text AS kind
          FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s
        UNION ALL
        SELECT c.relname, c.relkind::text FROM pg_depend d JOIN pg_class c ON c.oid = d.objid
         WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid = %s AND d.deptype IN ('a', 'i') AND c.relkind = 'S'
         ORDER BY 2, 1
        """,
        [t.oid, t.oid],
    )
    deps: list[Any] = await cur.fetchall()
    cur = await conn.execute(
        """
        SELECT conrelid::regclass::text AS child, conname FROM pg_constraint
         WHERE contype = 'f' AND confrelid = %s AND conrelid <> %s ORDER BY 1, 2
        """,
        [t.oid, t.oid],
    )
    inbound = [dict(r) for r in await cur.fetchall()]
    cur = await conn.execute(
        """
        SELECT DISTINCT v.oid::regclass::text AS view
          FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class
         WHERE d.classid = 'pg_rewrite'::regclass AND d.refobjid = %s AND v.oid <> %s
         ORDER BY 1
        """,
        [t.oid, t.oid],
    )
    views = [r["view"] for r in await cur.fetchall()]
    # Only the views the read-only role can read now get their grant taken away and given back.
    cur = await conn.execute(
        "SELECT v FROM unnest(%s::text[]) AS v"
        " WHERE has_table_privilege('mulligan_ro', v::regclass, 'SELECT')",
        [views],
    )
    ro_views = [r["v"] for r in await cur.fetchall()]
    cur = await conn.execute("SELECT obj_description(%s::oid, 'pg_class') AS note", [t.oid])
    note: Any = await cur.fetchone()
    renamed = []
    for n, d in enumerate(deps, 1):
        kw = "INDEX" if d["kind"] == "i" else "SEQUENCE"
        new = f"{prefix}_{kw[0].lower()}{n}"
        await conn.execute(
            sql.SQL("ALTER {} {} RENAME TO {}").format(
                sql.SQL(kw), sql.Identifier(t.schema, d["name"]), sql.Identifier(new)
            )
        )
        renamed.append({"kind": kw.lower(), "name": d["name"], "trash_name": new})
    for view in ro_views:
        await conn.execute(sql.SQL("REVOKE SELECT ON {} FROM mulligan_ro").format(sql.SQL(view)))
    await conn.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(t.ident, sql.Identifier(trash_table))
    )
    await conn.execute(
        sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
            sql.Identifier(t.schema, trash_table), sql.Identifier(TRASH_SCHEMA)
        )
    )
    # So messages about the trashed table (cascade counts, FK errors) can use its old names.
    trashed = {
        "schema": t.schema,
        "name": t.name,
        "renamed": {r["trash_name"]: r["name"] for r in renamed},
    }
    await conn.execute(
        sql.SQL("COMMENT ON TABLE {} IS {}").format(
            sql.Identifier(TRASH_SCHEMA, trash_table),
            sql.Literal(json.dumps({TRASH_NOTE: trashed})),
        )
    )
    return {
        "table_oid": t.oid,
        "schema": t.schema,
        "name": t.name,
        "trash_schema": TRASH_SCHEMA,
        "trash_name": trash_table,
        "renamed": renamed,
        "inbound_fks": inbound,
        "dependent_views": views,
        "ro_views": ro_views,
        "comment": note["note"],  # put back on undrop
    }


async def pg_query(
    ro: AsyncConnection[Any], query: str, *, timeout_ms: int, max_rows: int
) -> dict[str, Any]:
    """Runs as mulligan_ro in a READ ONLY transaction with a local statement_timeout. A
    server-side cursor goes over the extended protocol, so the query must be exactly one
    statement, and only max_rows + 1 rows are fetched."""
    async with ro.transaction():
        await ro.execute("SET TRANSACTION READ ONLY")
        await ro.execute("SELECT set_config('statement_timeout', %s, true)", [str(int(timeout_ms))])
        async with ro.cursor(name="pg_query") as cur:
            await cur.execute(query)
            rows = await cur.fetchmany(max_rows + 1)
            columns = [d.name for d in cur.description or []]
    return {
        "columns": columns,
        "rows": [list(r.values()) if isinstance(r, dict) else list(r) for r in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
    }
