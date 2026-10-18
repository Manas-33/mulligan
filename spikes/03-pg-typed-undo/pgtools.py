"""Typed Postgres tools, audit capture and undo for spike 0.3. Throwaway code.

Every write tool builds parameterized SQL from a table name, a structured filter
([{column, op, value}]) and values. Agent input is never spliced into SQL text: names
are checked against the catalog and quoted with sql.Identifier, and values go in as one
jsonb parameter that Postgres converts with jsonb_populate_record (the same conversion
undo uses to put old rows back).
"""

from __future__ import annotations

import hashlib
import heapq
import json
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from functools import partial
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb, set_json_loads

ALLOWED_SCHEMAS = ("public",)
TRASH_SCHEMA = "mulligan_trash"
AUDIT_TRIGGER = "mulligan_audit_row"
MAX_IDENT_BYTES = 63  # NAMEDATALEN - 1
PG_QUERY_TIMEOUT_MS = 5000
PG_QUERY_ROW_CAP = 1000


class ToolError(Exception):
    """Bad tool input. The agent sees this message."""


class UndoError(Exception):
    """An undo step could not run (missing row, FK cycle, name already taken)."""


# Ids ----------------------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_CROCKFORD[(n >> s) & 31] for s in range(125, -1, -5))


def new_action_id() -> str:
    return "act_" + ulid()


# Connections ---------------------------------------------------------------------------


def connect(conninfo: str, **kwargs: Any) -> psycopg.Connection:
    conn = psycopg.connect(conninfo, autocommit=True, row_factory=dict_row, **kwargs)
    # The default jsonb loader turns every JSON number into a float. Keep numerics exact.
    set_json_loads(partial(json.loads, parse_float=Decimal), conn)
    return conn


# Catalog -------------------------------------------------------------------------------


@dataclass
class Column:
    name: str
    type: str  # format_type() output, already quoted by Postgres
    generated: bool  # GENERATED ALWAYS AS (expr) STORED
    identity: str  # '' | 'a' (ALWAYS) | 'd' (BY DEFAULT)


@dataclass
class Table:
    oid: int
    schema: str
    name: str
    pk: list[str]
    columns: dict[str, Column]

    @property
    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.schema, self.name)

    @property
    def qualname(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def path(self) -> str:
        """Table segment of an after_hashes key: bare name in public, schema.name elsewhere."""
        return _seg(self.name if self.schema == "public" else self.qualname)

    @property
    def writable(self) -> list[str]:
        return [c.name for c in self.columns.values() if not c.generated]

    def pk_match(self, left: str, right: str) -> sql.Composable:
        return sql.SQL(" AND ").join(
            sql.SQL("{} = {}").format(sql.Identifier(left, c), sql.Identifier(right, c)) for c in self.pk
        )


def load_table(conn: psycopg.Connection, oid: int) -> Table:
    row = conn.execute(
        """
        SELECT c.oid, n.nspname AS schema, c.relname AS name,
               coalesce((SELECT array_agg(a.attname::text ORDER BY k.ord)
                           FROM pg_index i
                          CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
                           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                          WHERE i.indrelid = c.oid AND i.indisprimary), '{}') AS pk
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.oid = %s AND c.relkind IN ('r', 'p')
        """,
        [oid],
    ).fetchone()
    if row is None:
        raise UndoError(f"table oid {oid} no longer exists")
    cols = conn.execute(
        """
        SELECT attname AS name, format_type(atttypid, atttypmod) AS type,
               attgenerated <> '' AS generated, attidentity::text AS identity
          FROM pg_attribute
         WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped
         ORDER BY attnum
        """,
        [oid],
    ).fetchall()
    return Table(row["oid"], row["schema"], row["name"], list(row["pk"]), {c["name"]: Column(**c) for c in cols})


def resolve_table(conn: psycopg.Connection, table: str) -> Table:
    schema, _, name = table.rpartition(".")
    schema = schema or "public"
    if schema not in ALLOWED_SCHEMAS:
        raise ToolError(f"schema {schema!r} is not available")
    row = conn.execute(
        """
        SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
        """,
        [schema, name],
    ).fetchone()
    if row is None:
        raise ToolError(f"no table {table!r}")
    return load_table(conn, row["oid"])


def unaudited_tables(conn: psycopg.Connection, t: Table, op: str) -> list[str]:
    """The table plus every table an FK action (CASCADE / SET NULL / SET DEFAULT) can
    reach from it, minus those with the audit trigger. Writes are refused if any are
    left, because their changes could not be undone."""
    action_col = {"DELETE": "confdeltype", "UPDATE": "confupdtype"}.get(op)
    if action_col is None:
        reach = sql.SQL("SELECT %s::oid AS oid")
    else:
        reach = sql.SQL(
            """
            WITH RECURSIVE closure(oid) AS (
              SELECT %s::oid
              UNION
              SELECT con.conrelid FROM pg_constraint con JOIN closure ON con.confrelid = closure.oid
               WHERE con.contype = 'f' AND con.{} IN ('c', 'n', 'd')
            )
            SELECT oid FROM closure
            """
        ).format(sql.Identifier(action_col))
    q = sql.SQL(
        """
        SELECT r.oid::regclass::text AS t FROM ({}) r
         WHERE NOT EXISTS (SELECT 1 FROM pg_trigger tg
                            WHERE tg.tgrelid = r.oid AND tg.tgname = %s AND tg.tgenabled <> 'D')
        """
    ).format(reach)
    return [r["t"] for r in conn.execute(q, [t.oid, AUDIT_TRIGGER]).fetchall()]


def fk_edges(conn: psycopg.Connection, oids: list[int]) -> list[dict]:
    """FKs among the given tables: child (conrelid) -> parent (confrelid), with column names."""
    return conn.execute(
        """
        SELECT con.conrelid AS child, con.confrelid AS parent, con.conname,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(con.conkey) WITH ORDINALITY AS k(n, ord)
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n) AS cols,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(con.confkey) WITH ORDINALITY AS k(n, ord)
                  JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n) AS ref_cols
          FROM pg_constraint con
         WHERE con.contype = 'f' AND con.conrelid = ANY(%s::oid[]) AND con.confrelid = ANY(%s::oid[])
        """,
        [oids, oids],
    ).fetchall()


# Filters and values --------------------------------------------------------------------

_CMP = {"=": "=", "!=": "<>", "<>": "<>", "<": "<", "<=": "<=", ">": ">", ">=": ">="}


def _as_col(t: Table, col: str, json_expr: sql.Composable) -> sql.Composable:
    """A JSON value converted to the column's type by Postgres itself."""
    return sql.SQL("(jsonb_populate_record(NULL::{t}, jsonb_build_object({c}, {v}))).{ci}").format(
        t=t.ident, c=sql.Literal(col), v=json_expr, ci=sql.Identifier(col)
    )


def build_where(t: Table, filters: list[dict], all_rows: bool) -> tuple[sql.Composable, list]:
    if not filters:
        if not all_rows:
            raise ToolError("filter is empty; pass all=true to touch every row")
        return sql.SQL("TRUE"), []
    parts: list[sql.Composable] = []
    params: list[Any] = []
    for f in filters:
        col, op = f.get("column"), str(f.get("op", "")).strip().lower()
        if col not in t.columns:
            raise ToolError(f"unknown column {col!r} on {t.qualname}")
        c = sql.Identifier("tgt", col)
        if op in _CMP:
            parts.append(sql.SQL("{} {} {}").format(c, sql.SQL(_CMP[op]), _as_col(t, col, sql.SQL("%s::jsonb"))))
            params.append(Jsonb(f.get("value")))
        elif op in ("in", "not in"):
            if not isinstance(f.get("value"), list):
                raise ToolError(f"{op!r} needs a list value")
            arr = sql.SQL("ARRAY(SELECT {} FROM jsonb_array_elements(%s::jsonb) AS e)").format(
                _as_col(t, col, sql.SQL("e"))
            )
            tmpl = "{} = ANY({})" if op == "in" else "{} <> ALL({})"
            parts.append(sql.SQL(tmpl).format(c, arr))
            params.append(Jsonb(f["value"]))
        elif op in ("like", "ilike"):
            parts.append(sql.SQL("{}::text {} %s::text").format(c, sql.SQL(op.upper())))
            params.append(str(f.get("value")))
        elif op in ("is null", "is not null"):
            parts.append(sql.SQL("{} {}").format(c, sql.SQL(op.upper())))
        else:
            raise ToolError(f"unknown op {op!r}")
    return sql.SQL(" AND ").join(parts), params


# Action results ------------------------------------------------------------------------


@dataclass
class ActionResult:
    action_id: str
    tool: str
    rowcount: int
    audit: list[dict]
    after_hashes: dict[str, str]
    inverse: dict
    hash_refs: dict[str, tuple[int | None, str | None]] = field(default_factory=dict)
    # hash_refs: key -> (audit row id, column or None for a whole row). Drops use (None, None).
    direct_audit_ids: list[int] = field(default_factory=list)
    # Audit rows for rows the statement itself touched (from RETURNING). The rest came from
    # FK actions (CASCADE, SET NULL); the trigger can't tell them apart on its own.


def _seg(s: str) -> str:
    return s.replace("%", "%25").replace("/", "%2F").replace(",", "%2C")


def _pk_seg(t: Table, pk: dict) -> str:
    vals = []
    for c in t.pk:
        v = pk[c]
        vals.append(_seg(v if isinstance(v, str) else json.dumps(v, default=str)))
    return ",".join(vals)


def fetch_audit(conn: psycopg.Connection, action_id: str) -> list[dict]:
    return conn.execute(
        "SELECT * FROM mulligan_audit.audit_log WHERE action_id = %s ORDER BY id", [action_id]
    ).fetchall()


def _after_hashes(conn: psycopg.Connection, action_id: str, tables: dict[int, Table]):
    """One hash per (row, column) the action changed, computed in SQL so the same
    canonical form is used at capture and at conflict-check time. INSERT: every column.
    UPDATE: changed columns. DELETE: the whole row, hashed as absent. Generated columns
    are skipped (they can't be written or restored). Later audit rows win."""
    rows = conn.execute(
        """
        SELECT a.id, a.table_oid, a.op, a.pk, c.col,
               mulligan_audit.field_hash(CASE WHEN a.op = 'DELETE' THEN NULL ELSE a.new_row -> c.col END) AS h
          FROM mulligan_audit.audit_log a
         CROSS JOIN LATERAL (
               SELECT unnest(a.changed) WHERE a.op = 'UPDATE'
               UNION ALL SELECT jsonb_object_keys(a.new_row) WHERE a.op = 'INSERT'
               UNION ALL SELECT NULL::text WHERE a.op = 'DELETE') AS c(col)
         WHERE a.action_id = %s
         ORDER BY a.id, c.col
        """,
        [action_id],
    ).fetchall()
    hashes: dict[str, str] = {}
    refs: dict[str, tuple[int | None, str | None]] = {}
    for r in rows:
        if r["table_oid"] not in tables:
            tables[r["table_oid"]] = load_table(conn, r["table_oid"])
        t = tables[r["table_oid"]]
        if r["col"] is not None and t.columns[r["col"]].generated:
            continue
        key = f"postgres:{t.path}/{_pk_seg(t, r['pk'])}"
        if r["col"] is not None:
            key += f"/{_seg(r['col'])}"
        hashes[key] = r["h"]
        refs[key] = (r["id"], r["col"])
    return hashes, refs


def _pk_key(t: Table, row: dict) -> str:
    return json.dumps([row[c] for c in t.pk], default=str)


def _run_write(conn: psycopg.Connection, tool: str, action_id: str, t: Table, q: sql.Composable, params: list):
    q = q + sql.SQL(" RETURNING mulligan_audit.canon_row(tgt) AS j")
    with conn.transaction():
        # SET LOCAL can't take a bind parameter; set_config(..., true) is the same thing.
        conn.execute("SELECT set_config('mulligan.action_id', %s, true)", [action_id])
        cur = conn.execute(q, params)
        rowcount = cur.rowcount
        direct = {_pk_key(t, r["j"]) for r in cur.fetchall()}
        audit = fetch_audit(conn, action_id)
        hashes, refs = _after_hashes(conn, action_id, {t.oid: t})
    direct_ids = [a["id"] for a in audit if a["table_oid"] == t.oid and _pk_key(t, a["pk"]) in direct]
    inverse = {
        "kind": "pg_audit",
        "payload": {
            "action_id": action_id,
            "audit_ids": [audit[0]["id"], audit[-1]["id"]] if audit else [],
            "tables": sorted({f"{a['schema_name']}.{a['table_name']}" for a in audit}),
        },
    }
    return ActionResult(action_id, tool, rowcount, audit, hashes, inverse, refs, direct_ids)


# Write tools ---------------------------------------------------------------------------


def _check_audited(conn: psycopg.Connection, t: Table, op: str) -> None:
    if not t.pk:
        raise ToolError(f"{t.qualname} has no primary key, so writes to it can't be undone")
    missing = unaudited_tables(conn, t, op)
    if missing:
        raise ToolError(f"refusing {op}: no audit trigger on {', '.join(missing)}")


def pg_insert_rows(conn, table: str, rows: list[dict], *, action_id: str | None = None) -> ActionResult:
    t = resolve_table(conn, table)
    _check_audited(conn, t, "INSERT")
    if not rows:
        raise ToolError("rows is empty")
    cols = list(rows[0])
    if any(list(r) != cols for r in rows) or not set(cols) <= set(t.columns):
        raise ToolError("every row needs the same, existing columns")
    ci = sql.SQL(", ").join(map(sql.Identifier, cols))
    q = sql.SQL("INSERT INTO {t} AS tgt ({c}) SELECT {c} FROM jsonb_populate_recordset(NULL::{t}, %s::jsonb)").format(
        t=t.ident, c=ci
    )
    return _run_write(conn, "pg_insert_rows", action_id or new_action_id(), t, q, [Jsonb(rows)])


def pg_update_rows(
    conn, table: str, filter: list[dict], values: dict, *, all: bool = False, action_id: str | None = None
) -> ActionResult:
    t = resolve_table(conn, table)
    _check_audited(conn, t, "UPDATE")
    if not values or not set(values) <= set(t.writable):
        raise ToolError("values must name existing, writable columns")
    where, wparams = build_where(t, filter, all)
    sets = sql.SQL(", ").join(sql.SQL("{c} = _v.{c}").format(c=sql.Identifier(c)) for c in values)
    q = sql.SQL("UPDATE {t} AS tgt SET {s} FROM jsonb_populate_record(NULL::{t}, %s::jsonb) AS _v WHERE {w}").format(
        t=t.ident, s=sets, w=where
    )
    return _run_write(conn, "pg_update_rows", action_id or new_action_id(), t, q, [Jsonb(values), *wparams])


def pg_delete_rows(
    conn, table: str, filter: list[dict], *, all: bool = False, action_id: str | None = None
) -> ActionResult:
    t = resolve_table(conn, table)
    _check_audited(conn, t, "DELETE")
    where, wparams = build_where(t, filter, all)
    q = sql.SQL("DELETE FROM {t} AS tgt WHERE {w}").format(t=t.ident, w=where)
    return _run_write(conn, "pg_delete_rows", action_id or new_action_id(), t, q, wparams)


def fit_ident(s: str) -> str:
    """Cut to 63 bytes on a UTF-8 character boundary. Postgres would also cut a longer
    name, but silently (only a NOTICE), which would chop the action id off the end."""
    return s.encode()[:MAX_IDENT_BYTES].decode("utf-8", "ignore")


def trash_names(action_id: str, table: str) -> tuple[str, str]:
    """(table name in trash, prefix for its indexes and sequences). The action id goes
    first so truncation never cuts the unique part. Lowercase so it works unquoted."""
    prefix = action_id.lower()
    return fit_ident(f"{prefix}__{table}"), prefix


def pg_drop_table(conn, table: str, *, action_id: str | None = None) -> ActionResult:
    """Soft drop: rename the table and every index and owned sequence to action-prefixed
    names, then move the table into mulligan_trash (indexes and owned sequences move with
    it). Renaming the dependents avoids name clashes in the trash when a table with the
    same name is dropped twice."""
    action_id = action_id or new_action_id()
    t = resolve_table(conn, table)
    trash_table, prefix = trash_names(action_id, t.name)
    deps = conn.execute(
        """
        SELECT c.relname AS name, c.relkind::text AS kind FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
         WHERE i.indrelid = %s
        UNION ALL
        SELECT c.relname, c.relkind::text FROM pg_depend d JOIN pg_class c ON c.oid = d.objid
         WHERE d.classid = 'pg_class'::regclass AND d.refclassid = 'pg_class'::regclass
           AND d.refobjid = %s AND d.deptype IN ('a', 'i') AND c.relkind = 'S'
         ORDER BY 2, 1
        """,
        [t.oid, t.oid],
    ).fetchall()
    inbound = conn.execute(
        """
        SELECT conrelid::regclass::text AS child, conname FROM pg_constraint
         WHERE contype = 'f' AND confrelid = %s AND conrelid <> %s ORDER BY 1, 2
        """,
        [t.oid, t.oid],
    ).fetchall()
    renamed = []
    with conn.transaction():
        conn.execute("SELECT set_config('mulligan.action_id', %s, true)", [action_id])
        for n, d in enumerate(deps, 1):
            kw = "INDEX" if d["kind"] == "i" else "SEQUENCE"
            new = f"{prefix}_{kw[0].lower()}{n}"
            conn.execute(
                sql.SQL("ALTER {} {} RENAME TO {}").format(
                    sql.SQL(kw), sql.Identifier(t.schema, d["name"]), sql.Identifier(new)
                )
            )
            renamed.append({"kind": kw.lower(), "name": d["name"], "trash_name": new})
        conn.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(t.ident, sql.Identifier(trash_table)))
        conn.execute(
            sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
                sql.Identifier(t.schema, trash_table), sql.Identifier(TRASH_SCHEMA)
            )
        )
        absent = conn.execute("SELECT mulligan_audit.field_hash(NULL) AS h").fetchone()["h"]
    inverse = {
        "kind": "pg_undrop",
        "payload": {
            "table_oid": t.oid,
            "schema": t.schema,
            "name": t.name,
            "trash_schema": TRASH_SCHEMA,
            "trash_name": trash_table,
            "renamed": renamed,
            "inbound_fks": inbound,
        },
    }
    key = f"postgres:{t.path}"
    return ActionResult(action_id, "pg_drop_table", 0, [], {key: absent}, inverse, {key: (None, None)})


# Read-only query -----------------------------------------------------------------------


def pg_query(ro: psycopg.Connection, query: str, *, timeout_ms: int = PG_QUERY_TIMEOUT_MS,
             row_cap: int = PG_QUERY_ROW_CAP) -> dict:
    """Runs as mulligan_ro in a READ ONLY transaction with a local statement_timeout.
    A server-side cursor (DECLARE ... CURSOR FOR <query>) is sent over the extended
    protocol, so the query must be exactly one SELECT/VALUES statement, and only
    row_cap + 1 rows are ever fetched."""
    with ro.transaction():
        ro.execute("SET TRANSACTION READ ONLY")
        ro.execute("SELECT set_config('statement_timeout', %s, true)", [str(int(timeout_ms))])
        with ro.cursor(name="pg_query") as cur:
            cur.execute(query)  # type: ignore[arg-type]
            rows = cur.fetchmany(row_cap + 1)
            columns = [d.name for d in cur.description or []]
    return {"columns": columns, "rows": rows[:row_cap], "truncated": len(rows) > row_cap}


# Hash checks ---------------------------------------------------------------------------


def current_hashes(conn: psycopg.Connection, result: ActionResult) -> dict[str, str]:
    """Recompute every after_hashes key from the live database (what Phase 3's conflict
    check does)."""
    out: dict[str, str] = {}
    if result.tool == "pg_drop_table":
        p = result.inverse["payload"]
        row = conn.execute(
            """
            SELECT mulligan_audit.field_hash(
                     CASE WHEN to_regclass(format('%%I.%%I', %s::text, %s::text)) IS NULL THEN NULL
                          ELSE to_jsonb(to_regclass(format('%%I.%%I', %s::text, %s::text))::oid) END) AS h
            """,
            [p["schema"], p["name"], p["schema"], p["name"]],
        ).fetchone()
        return {k: row["h"] for k in result.after_hashes}
    tables: dict[int, Table] = {}
    by_id = {a["id"]: a for a in result.audit}
    for key, (audit_id, col) in result.hash_refs.items():
        oid = by_id[audit_id]["table_oid"]
        if oid not in tables:
            tables[oid] = load_table(conn, oid)
        t = tables[oid]
        q = sql.SQL(
            """
            SELECT mulligan_audit.field_hash(CASE WHEN %s::text IS NULL THEN cur.j ELSE cur.j -> %s::text END) AS h
              FROM mulligan_audit.audit_log a
              LEFT JOIN LATERAL (
                   SELECT mulligan_audit.canon_row(tgt) AS j FROM {t} AS tgt
                    CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.pk) AS w
                    WHERE {m}) AS cur ON true
             WHERE a.id = %s
            """
        ).format(t=t.ident, m=t.pk_match("tgt", "w"))
        out[key] = conn.execute(q, [col, col, audit_id]).fetchone()["h"]
    return out


# Undo ----------------------------------------------------------------------------------


def toposort(nodes: list, parents: dict) -> list:
    """Kahn's algorithm; parents come first. Ties keep the input order. Raises on a cycle."""
    rank = {n: i for i, n in enumerate(nodes)}
    waiting = {n: {p for p in parents.get(n, ()) if p in rank and p != n} for n in nodes}
    children = defaultdict(list)
    for n, ps in waiting.items():
        for p in ps:
            children[p].append(n)
    ready = [(rank[n], n) for n in nodes if not waiting[n]]
    heapq.heapify(ready)
    out = []
    while ready:
        _, n = heapq.heappop(ready)
        out.append(n)
        for c in children[n]:
            waiting[c].discard(n)
            if not waiting[c]:
                heapq.heappush(ready, (rank[c], c))
    if len(out) != len(nodes):
        raise UndoError(f"FK cycle among {[n for n in nodes if n not in out]}")
    return out


def order_rows(rows: list[dict], self_fks: list[dict], image: str) -> list[dict]:
    """Rows of one table, parents first along self-referencing FKs (e.g. employees.manager_id).
    image is 'old_row' (re-inserts) or 'new_row' (deleting inserted rows)."""
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


def revert_row(conn: psycopg.Connection, t: Table, a: dict) -> str:
    """Invert one audit row. Values never leave Postgres: each statement reads the
    audit row by id and converts its jsonb back to the column types."""
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
        # OVERRIDING SYSTEM VALUE puts back GENERATED ALWAYS identity values (and is
        # accepted on tables without one). Generated stored columns are left out.
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
        cols = [c for c in a["changed"] if c in t.columns and not t.columns[c].generated]
        if not cols:
            return f"skip {t.name} {a['pk']} (only generated columns changed)"
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
    n = conn.execute(q, [a["id"]]).rowcount
    if n != 1:
        raise UndoError(f"{step}: expected 1 row, got {n}")
    return step


def undo_action(conn: psycopg.Connection, result: ActionResult, *, undo_id: str) -> dict:
    """Undo one action in one transaction. Order: (1) delete rows it inserted, children
    first; (2) re-insert rows it deleted, parents first by FK dependency (tables sorted
    from pg_constraint, rows of a self-referencing table sorted by their own FK values);
    (3) put back columns it updated, parents first, newest audit row first."""
    if result.inverse["kind"] == "pg_undrop":
        return undo_drop(conn, result.inverse["payload"], undo_id=undo_id)
    audit = fetch_audit(conn, result.action_id)
    steps: list[str] = []
    with conn.transaction():
        conn.execute("SELECT set_config('mulligan.action_id', %s, true)", [undo_id])
        first_seen: dict[int, int] = {}
        for a in audit:
            first_seen.setdefault(a["table_oid"], a["id"])
        oids = list(first_seen)
        tables = {o: load_table(conn, o) for o in oids}
        parents = defaultdict(set)
        self_fks = defaultdict(list)
        for e in fk_edges(conn, oids):
            if e["child"] == e["parent"]:
                self_fks[e["child"]].append(e)
            else:
                parents[e["child"]].add(e["parent"])
        order = toposort(oids, parents)
        by = defaultdict(list)
        for a in audit:
            by[(a["table_oid"], a["op"])].append(a)
        for oid in reversed(order):
            for a in reversed(order_rows(by[(oid, "INSERT")], self_fks[oid], "new_row")):
                steps.append(revert_row(conn, tables[oid], a))
        for oid in order:
            for a in order_rows(by[(oid, "DELETE")], self_fks[oid], "old_row"):
                steps.append(revert_row(conn, tables[oid], a))
        for oid in order:
            for a in reversed(by[(oid, "UPDATE")]):
                steps.append(revert_row(conn, tables[oid], a))
    return {"table_order": [tables[o].name for o in order], "steps": steps}


def undo_drop(conn: psycopg.Connection, p: dict, *, undo_id: str) -> dict:
    with conn.transaction():
        conn.execute("SELECT set_config('mulligan.action_id', %s, true)", [undo_id])
        row = conn.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s",
            [p["table_oid"]],
        ).fetchone()
        if row is None or (row["nspname"], row["relname"]) != (p["trash_schema"], p["trash_name"]):
            raise UndoError(f"trashed table {p['trash_name']} is gone or was moved")
        taken = [
            n for n in [p["name"], *(r["name"] for r in p["renamed"])]
            if conn.execute("SELECT to_regclass(format('%%I.%%I', %s::text, %s::text)) AS r", [p["schema"], n]).fetchone()["r"]
        ]
        if taken:
            raise UndoError(f"names already taken in {p['schema']}: {taken}")
        for r in p["renamed"]:
            conn.execute(
                sql.SQL("ALTER {} {} RENAME TO {}").format(
                    sql.SQL(r["kind"].upper()),
                    sql.Identifier(p["trash_schema"], r["trash_name"]),
                    sql.Identifier(r["name"]),
                )
            )
        conn.execute(
            sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                sql.Identifier(p["trash_schema"], p["trash_name"]), sql.Identifier(p["name"])
            )
        )
        conn.execute(
            sql.SQL("ALTER TABLE {} SET SCHEMA {}").format(
                sql.Identifier(p["trash_schema"], p["name"]), sql.Identifier(p["schema"])
            )
        )
    return {"table_order": [p["name"]], "steps": [f"undrop {p['schema']}.{p['name']}"]}


# Checksums and structure ---------------------------------------------------------------


def checksums(conn: psycopg.Connection, schema: str = "public") -> dict[str, str]:
    """sha256 per table over its canonical rows, sorted bytewise (COLLATE "C"), plus a
    combined '*' hash. Sequences are not included: undo doesn't rewind them."""
    out = {}
    tables = conn.execute(
        """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind IN ('r', 'p') ORDER BY c.relname COLLATE "C"
        """,
        [schema],
    ).fetchall()
    for r in tables:
        q = sql.SQL(
            """
            SELECT coalesce(string_agg(j, E'\\n' ORDER BY j COLLATE "C"), '') AS body
              FROM (SELECT mulligan_audit.canon_row(tgt)::text AS j FROM {} AS tgt) s
            """
        ).format(sql.Identifier(schema, r["relname"]))
        body = conn.execute(q).fetchone()["body"]
        out[r["relname"]] = hashlib.sha256(body.encode()).hexdigest()
    out["*"] = hashlib.sha256("\n".join(f"{k}:{v}" for k, v in out.items()).encode()).hexdigest()
    return out


def table_structure(conn: psycopg.Connection, oid: int) -> dict:
    """What a dropped table must come back with: columns, constraints (PK, unique, FKs
    out), indexes, inbound FKs, identity sequence, triggers, grants."""
    q = lambda s: [tuple(r.values()) for r in conn.execute(s, [oid]).fetchall()]  # noqa: E731
    return {
        "name": q("SELECT relnamespace::regnamespace::text, relname, relowner::regrole::text, relacl::text FROM pg_class WHERE oid = %s"),
        "columns": q(
            """SELECT attname, format_type(atttypid, atttypmod), attnotnull, attidentity::text, attgenerated::text,
                      pg_get_expr(d.adbin, d.adrelid)
                 FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum"""
        ),
        "constraints": q("SELECT conname, contype::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s ORDER BY 1"),
        "indexes": q(
            """SELECT c.relname, c.relnamespace::regnamespace::text, pg_get_indexdef(i.indexrelid)
                 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE i.indrelid = %s ORDER BY 1"""
        ),
        "inbound_fks": q(
            """SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) FROM pg_constraint
                WHERE contype = 'f' AND confrelid = %s AND conrelid <> confrelid ORDER BY 1, 2"""
        ),
        "sequences": q(
            """SELECT c.relname, c.relnamespace::regnamespace::text, d.deptype::text, pg_sequence_last_value(c.oid)
                 FROM pg_depend d JOIN pg_class c ON c.oid = d.objid
                WHERE d.classid = 'pg_class'::regclass AND d.refobjid = %s AND c.relkind = 'S' ORDER BY 1"""
        ),
        "triggers": q("SELECT tgname, tgenabled::text FROM pg_trigger WHERE tgrelid = %s AND NOT tgisinternal ORDER BY 1"),
    }
