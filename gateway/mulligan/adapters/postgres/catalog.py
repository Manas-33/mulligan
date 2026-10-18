"""What the Postgres adapter reads from pg_catalog: tables, columns, keys and FKs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection, sql

TRASH_SCHEMA = "mulligan_trash"
AUDIT_TRIGGER = "mulligan_audit_row"
HIDDEN_SCHEMAS = frozenset({TRASH_SCHEMA, "mulligan_audit", "pg_catalog", "information_schema"})
MAX_IDENT_BYTES = 63  # NAMEDATALEN - 1
TRASH_NOTE = "mulligan_trashed"  # key of the JSON comment on a trashed table


class ToolError(Exception):
    """Bad tool input. The agent sees this message."""


class UndoError(Exception):
    """An undo step couldn't run (missing row, FK cycle, name already taken)."""


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    generated: bool  # GENERATED ALWAYS AS (expr) STORED
    identity: str  # '' | 'a' (ALWAYS) | 'd' (BY DEFAULT)


@dataclass(frozen=True)
class Table:
    oid: int
    schema: str
    name: str
    pk: tuple[str, ...]
    columns: dict[str, Column]

    @property
    def ident(self) -> sql.Identifier:
        return sql.Identifier(self.schema, self.name)

    @property
    def qualname(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def path(self) -> str:
        """Table segment of a field path: bare name in public, schema.name elsewhere."""
        return seg(self.name if self.schema == "public" else self.qualname)

    @property
    def writable(self) -> list[str]:
        return [c.name for c in self.columns.values() if not c.generated]

    def pk_match(self, left: str, right: str) -> sql.Composable:
        return sql.SQL(" AND ").join(
            sql.SQL("{} = {}").format(sql.Identifier(left, c), sql.Identifier(right, c))
            for c in self.pk
        )

    def row_key(self, pk: dict[str, Any]) -> str:
        return f"postgres:{self.path}/{pk_seg(self, pk)}"


def seg(s: str) -> str:
    return s.replace("%", "%25").replace("/", "%2F").replace(",", "%2C")


def pk_seg(t: Table, pk: dict[str, Any]) -> str:
    return ",".join(
        seg(v if isinstance(v, str) else json.dumps(v, default=str)) for v in (pk[c] for c in t.pk)
    )


def fit_ident(s: str) -> str:
    """Cut to 63 bytes on a UTF-8 boundary. Postgres would cut silently, losing the end."""
    return s.encode()[:MAX_IDENT_BYTES].decode("utf-8", "ignore")


async def load_table(conn: AsyncConnection[Any], oid: int) -> Table:
    cur = await conn.execute(
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
    )
    row: Any = await cur.fetchone()
    if row is None:
        raise UndoError(f"table oid {oid} no longer exists")
    cur = await conn.execute(
        """
        SELECT attname AS name, format_type(atttypid, atttypmod) AS type,
               attgenerated <> '' AS generated, attidentity::text AS identity
          FROM pg_attribute
         WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped
         ORDER BY attnum
        """,
        [oid],
    )
    cols: list[Any] = await cur.fetchall()
    return Table(
        oid=row["oid"],
        schema=row["schema"],
        name=row["name"],
        pk=tuple(row["pk"]),
        columns={c["name"]: Column(**c) for c in cols},
    )


async def resolve_table(
    conn: AsyncConnection[Any], table: str, allowed_schemas: tuple[str, ...]
) -> Table:
    schema, _, name = table.rpartition(".")
    schema = schema or "public"
    if schema not in allowed_schemas or schema in HIDDEN_SCHEMAS:
        raise ToolError(f"schema {schema!r} is not available")
    cur = await conn.execute(
        """
        SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relname = %s AND c.relkind IN ('r', 'p')
        """,
        [schema, name],
    )
    row: Any = await cur.fetchone()
    if row is None:
        raise ToolError(f"no table {table!r}")
    return await load_table(conn, row["oid"])


async def unaudited_tables(conn: AsyncConnection[Any], t: Table, op: str) -> list[str]:
    """The table plus every table an FK action (CASCADE / SET NULL / SET DEFAULT) can reach
    from it, minus those with the audit trigger. Writes are refused if any are left."""
    action_col = {"DELETE": "confdeltype", "UPDATE": "confupdtype"}.get(op)
    if action_col is None:
        reach: sql.Composable = sql.SQL("SELECT %s::oid AS oid")
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
    query = sql.SQL(
        """
        SELECT r.oid::regclass::text AS t FROM ({}) r
         WHERE NOT EXISTS (SELECT 1 FROM pg_trigger tg
                            WHERE tg.tgrelid = r.oid AND tg.tgname = %s AND tg.tgenabled <> 'D')
        """
    ).format(reach)
    cur = await conn.execute(query, [t.oid, AUDIT_TRIGGER])
    return [r["t"] for r in await cur.fetchall()]


async def trash_aliases(conn: AsyncConnection[Any]) -> dict[str, str]:
    """Name in the trash -> name before the drop, for every trashed table and its renamed
    indexes and sequences. Read from the comment soft_drop puts on the trashed table."""
    cur = await conn.execute(
        """
        SELECT c.relname, obj_description(c.oid, 'pg_class') AS note
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = %s AND c.relkind IN ('r', 'p')
        """,
        [TRASH_SCHEMA],
    )
    aliases: dict[str, str] = {}
    for row in await cur.fetchall():
        try:
            note = json.loads(row["note"])[TRASH_NOTE]
            aliases[row["relname"]] = str(note["name"])
            aliases.update(note.get("renamed", {}))
        except (TypeError, ValueError, KeyError):
            # Dropped before the note existed: the name is "<action id>__<table>", maybe cut.
            aliases[row["relname"]] = row["relname"].partition("__")[2] or row["relname"]
    return aliases


def untrash(text: str, aliases: dict[str, str]) -> str:
    """Swap trashed names in text people see for the names they know."""
    for trashed in sorted(aliases, key=len, reverse=True):
        text = text.replace(trashed, aliases[trashed])
    return text


async def fk_edges(conn: AsyncConnection[Any], oids: list[int]) -> list[dict[str, Any]]:
    """FKs among the given tables: child (conrelid) -> parent (confrelid), with columns."""
    cur = await conn.execute(
        """
        SELECT con.conrelid AS child, con.confrelid AS parent, con.conname,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(con.conkey) WITH ORDINALITY AS k(n, ord)
                  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.n) AS cols,
               (SELECT array_agg(a.attname::text ORDER BY k.ord)
                  FROM unnest(con.confkey) WITH ORDINALITY AS k(n, ord)
                  JOIN pg_attribute a ON a.attrelid = con.confrelid AND a.attnum = k.n) AS ref_cols
          FROM pg_constraint con
         WHERE con.contype = 'f' AND con.conrelid = ANY(%s::oid[])
           AND con.confrelid = ANY(%s::oid[])
        """,
        [oids, oids],
    )
    return list(await cur.fetchall())
