"""Undo primitives: FK ordering and one-row inverse statements that read the audit row by id."""

from __future__ import annotations

import heapq
from collections import defaultdict
from collections.abc import Hashable, Sequence
from typing import Any

from psycopg import AsyncConnection, sql

from mulligan.adapters.postgres.catalog import Table, UndoError


def toposort[N: Hashable](nodes: Sequence[N], parents: dict[N, set[N]]) -> list[N]:
    """Kahn's algorithm; parents come first, ties keep the input order. Raises on a cycle."""
    rank = {n: i for i, n in enumerate(nodes)}
    waiting = {n: {p for p in parents.get(n, ()) if p in rank and p != n} for n in nodes}
    children: dict[N, list[N]] = defaultdict(list)
    for n, ps in waiting.items():
        for p in ps:
            children[p].append(n)
    ready = [(rank[n], i, n) for i, n in enumerate(nodes) if not waiting[n]]
    heapq.heapify(ready)
    out: list[N] = []
    while ready:
        _, _, n = heapq.heappop(ready)
        out.append(n)
        for c in children[n]:
            waiting[c].discard(n)
            if not waiting[c]:
                heapq.heappush(ready, (rank[c], rank[c], c))
    if len(out) != len(nodes):
        raise UndoError(f"FK cycle among {[n for n in nodes if n not in out]}")
    return out


def order_rows(
    rows: list[dict[str, Any]], self_fks: list[dict[str, Any]], image: str
) -> list[dict[str, Any]]:
    """Rows of one table, parents first along self-referencing FKs (e.g. employees.manager_id).
    `image` is 'old_row' (re-inserts) or 'new_row' (deleting inserted rows)."""
    if not self_fks or len(rows) < 2:
        return rows
    ids = [r["id"] for r in rows]
    parents: dict[int, set[int]] = defaultdict(set)
    for fk in self_fks:
        by_key = {tuple(r[image][c] for c in fk["ref_cols"]): r["id"] for r in rows}
        for r in rows:
            k = tuple(r[image][c] for c in fk["cols"])
            if None not in k and k in by_key and by_key[k] != r["id"]:
                parents[r["id"]].add(by_key[k])
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in toposort(ids, parents)]


async def revert_row(
    conn: AsyncConnection[Any], t: Table, a: dict[str, Any], cols: list[str] | None = None
) -> str:
    """Invert one audit row; for an UPDATE, only `cols`. Values never leave Postgres."""
    if a["op"] == "INSERT":
        q = sql.SQL(
            """
            DELETE FROM {t} AS tgt USING mulligan_audit.audit_log AS a
             CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.new_row) AS w
             WHERE a.id = %s AND {m}
            """
        ).format(t=t.ident, m=t.pk_match("tgt", "w"))
        step = f"delete {t.name} {a['pk']}"
    elif a["op"] == "DELETE":
        # OVERRIDING SYSTEM VALUE puts back GENERATED ALWAYS identity values.
        q = sql.SQL(
            """
            INSERT INTO {t} ({c}) OVERRIDING SYSTEM VALUE
            SELECT {rc} FROM mulligan_audit.audit_log AS a
             CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.old_row) AS r
             WHERE a.id = %s
            """
        ).format(
            t=t.ident,
            c=sql.SQL(", ").join(map(sql.Identifier, t.writable)),
            rc=sql.SQL(", ").join(sql.Identifier("r", c) for c in t.writable),
        )
        step = f"insert {t.name} {a['pk']}"
    else:
        cols = [c for c in (cols or []) if c in t.columns and not t.columns[c].generated]
        if not cols:
            return f"skip {t.name} {a['pk']}"
        q = sql.SQL(
            """
            UPDATE {t} AS tgt SET {s}
              FROM mulligan_audit.audit_log AS a
             CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.old_row) AS r
             CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.new_row) AS w
             WHERE a.id = %s AND {m}
            """
        ).format(
            t=t.ident,
            s=sql.SQL(", ").join(sql.SQL("{c} = r.{c}").format(c=sql.Identifier(c)) for c in cols),
            m=t.pk_match("tgt", "w"),
        )
        step = f"update {t.name} {a['pk']} {cols}"
    cur = await conn.execute(q, [a["id"]])
    if cur.rowcount != 1:
        raise UndoError(f"{step}: expected 1 row, got {cur.rowcount}")
    return step


async def undrop(conn: AsyncConnection[Any], p: dict[str, Any]) -> None:
    """Move a soft-dropped table and its renamed dependents back. Inside the caller's txn."""
    cur = await conn.execute(
        "SELECT n.nspname, c.relname FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s",
        [p["table_oid"]],
    )
    row: Any = await cur.fetchone()
    if row is None or (row["nspname"], row["relname"]) != (p["trash_schema"], p["trash_name"]):
        raise UndoError(f"trashed table {p['trash_name']} is gone or was moved")
    if "comment" in p:
        await conn.execute(
            sql.SQL("COMMENT ON TABLE {} IS {}").format(
                sql.Identifier(p["trash_schema"], p["trash_name"]), sql.Literal(p["comment"])
            )
        )
    for r in p["renamed"]:
        await conn.execute(
            sql.SQL("ALTER {} {} RENAME TO {}").format(
                sql.SQL(r["kind"].upper()),
                sql.Identifier(p["trash_schema"], r["trash_name"]),
                sql.Identifier(r["name"]),
            )
        )
    await conn.execute(
        sql.SQL("ALTER TABLE {} RENAME TO {}").format(
            sql.Identifier(p["trash_schema"], p["trash_name"]), sql.Identifier(p["name"])
        )
    )
    await conn.execute(
        sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
            sql.Identifier(p["trash_schema"], p["name"]), sql.Identifier(p["schema"])
        )
    )
    for view in p.get("dependent_views", []):
        await conn.execute(sql.SQL("GRANT SELECT ON {} TO mulligan_ro").format(sql.SQL(view)))


async def names_taken(conn: AsyncConnection[Any], p: dict[str, Any]) -> list[str]:
    taken = []
    for name in [p["name"], *(r["name"] for r in p["renamed"])]:
        cur = await conn.execute(
            "SELECT to_regclass(format('%%I.%%I', %s::text, %s::text)) AS r", [p["schema"], name]
        )
        row: Any = await cur.fetchone()
        if row["r"]:
            taken.append(name)
    return taken
