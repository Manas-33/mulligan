# SDK notes

Check each SDK against its current docs before you use it. Record the pinned version, the docs link, the date you checked, and any gotchas. Add entries below; don't rewrite old ones.

Template:

```
## <SDK> — <pinned version>
Docs: <url>  ·  Checked: YYYY-MM-DD by <A|B>
- Gotcha / decision
```

## Docs index
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Claude Code MCP: https://code.claude.com/docs/en/mcp · hooks: https://code.claude.com/docs/en/hooks
- Devin custom MCP: https://docs.devin.ai/work-with-devin/mcp
- Fetch ASI:One agent: https://uagents.fetch.ai/docs/examples/asi-1
- Photon spectrum-ts: https://photon.codes/docs/spectrum-ts/introduction
- Sentry MCP monitoring (Python): https://docs.sentry.io/platforms/python/tracing/instrumentation/custom-instrumentation/mcp-module/

## Entries

_(none yet. Phase 0 fills these in)_

## PostgreSQL + psycopg (spike 0.3, typed-tool undo) — PostgreSQL 16.15 on `timescale/timescaledb:latest-pg16` (TimescaleDB 2.30.2, digest `sha256:6f139d56…`), `psycopg[binary]==3.3.6`, Python 3.12
Docs: https://www.postgresql.org/docs/16/sql-altertable.html · https://www.postgresql.org/docs/16/sql-insert.html · https://www.postgresql.org/docs/16/functions-json.html · https://www.postgresql.org/docs/16/functions-admin.html · https://www.postgresql.org/docs/16/sql-declare.html · https://www.psycopg.org/psycopg3/docs/advanced/cursors.html#server-side-cursors · https://www.psycopg.org/psycopg3/docs/basic/from_pg2.html#multiple-statements-in-the-same-query · https://www.psycopg.org/psycopg3/docs/basic/adapt.html  ·  Checked: 2026-10-17 by A
- Code is in `spikes/03-pg-typed-undo/`. `run_spike.py` runs 44 checks, and all pass on two runs in a row. `mulligan_prod_setup.sql` holds the audit table, the trigger, the roles and `mulligan_trash` for `infra/db/prod-db/`.
- **PASS: checksum after undo equals checksum before.** This holds for each of 9 actions, undone newest first, and for the whole session. It includes a customer delete that cascaded into `orders`, `order_items` (a grandchild), `support_tickets` and `customer_notes` (`ON DELETE SET NULL`).
- **PASS: a dropped table comes back whole.** It returns with the same OID and its data, PK, unique and plain indexes, identity sequence (same last value), trigger, grants, and the inbound FK from `price_overrides`, which is still enforced. The catalog snapshot after undo equals the one taken before the drop.
- **PASS: `mulligan_ro` can't see the trash.** It has no USAGE on `mulligan_trash` or `mulligan_audit`. Selecting a trashed table fails with `permission denied for schema mulligan_trash`.
- **PASS: a runaway `pg_query` is killed.** `SELECT pg_sleep(10)` is cancelled by `statement_timeout` after 5.00 s. A 1 s per-call timeout, below the role's 5 s backstop, fires at 1.01 s.
- **Decision: sort by FK dependency, never `DEFERRABLE` FKs.** One action is undone in one transaction, in three passes. First, delete the rows it inserted, children first. Second, re-insert the rows it deleted, parents first, with tables topo-sorted from `pg_constraint` (`contype = 'f'`, both ends among the action's tables). Third, put back the columns it changed, parents first, newest audit row first. Replaying in plain reverse audit order fails with an FK violation for the 3-level cascade, the self-referencing cascade and a `p → c → gc` probe.
- **Self-referencing FK** (`employees.manager_id ON DELETE CASCADE`): a table-level sort can't order rows within one table. So that table's rows are topo-sorted by their own FK values: a row waits for the row whose key its FK points at. Deleting the root of a 7-person tree re-inserted ids 10, 3, 12, 7, 15, 1, 2. A row cycle (A manages B, B manages A) or a cycle between tables raises `UndoError`. It would need a two-phase insert (FK column NULL, then UPDATE), which only works on nullable columns.
- **A cascade is logged parent first.** FK actions queue their row events behind the outer statement's, so the parent's audit row comes first and its children follow level by level, whatever the audit trigger is named. `pg_trigger_depth()` is 1 for cascaded rows too, so it can't mark them. The write tools use `RETURNING mulligan_audit.canon_row(tgt)` to tag the rows the statement touched directly. Every other audit row in the action came from a cascade.
- **Re-inserting into `GENERATED ALWAYS AS IDENTITY`.** A plain insert fails with `cannot insert a non-DEFAULT value into column "id"` (428C9). This works, and Postgres also accepts it on tables with no identity column: `INSERT INTO t (<non-generated cols>) OVERRIDING SYSTEM VALUE SELECT r.<cols> FROM mulligan_audit.audit_log a CROSS JOIN LATERAL jsonb_populate_record(NULL::t, a.old_row) r WHERE a.id = $1`. Leave `GENERATED ALWAYS AS (...) STORED` columns out of the list. Identity sequences aren't rewound, so ids have gaps after undo. Checksums cover rows only.
- **Setting the action id.** `SET LOCAL` can't take a bind parameter, so use `SELECT set_config('mulligan.action_id', $1, true)`, which does the same thing. After that transaction ends, `current_setting('mulligan.action_id', true)` returns `''`, not NULL, in the same session. The trigger skips logging on both.
- **63-byte identifiers.** Postgres silently truncates longer names at a character boundary and only raises a NOTICE. `<table>__<action_id>` for a 58-byte table name became `…_channel_v2__act`, losing the id. Trash names are `<action_id, lowercased>__<table>`, cut to 63 bytes on a UTF-8 boundary, so the unique part always survives. The original name is kept in the inverse payload.
- **`ALTER TABLE ... SET SCHEMA`** moves the table's indexes, constraints and owned (identity or serial) sequences with it. Triggers, grants, the OID and inbound FKs (which point by OID) are unchanged. Index and sequence names are unique per schema, so moving a second table with the same name failed with `relation "dup_pkey" already exists in schema "mulligan_trash"`. The soft drop therefore renames every index and owned sequence to `<action_id>_i<n>` / `_s<n>` first, and undo renames them back. Renaming a PK or unique index renames its constraint too. Undo is a conflict if any original name is already taken in the target schema.
- **What the trash still exposes.** A view over the dropped table keeps working, and `mulligan_ro` read 12 trashed rows through `legacy_pricing_v`. Trashed table names (not their data) show up in `pg_class` and `information_schema.tables` for `mulligan_ro`, because its SELECT grant moves with the table. Child tables can still insert rows that reference the trashed table.
- **`pg_query` uses a psycopg named (server-side) cursor.** psycopg sends `DECLARE ... CURSOR FOR <sql>` over the extended protocol, which has four effects. A multi-statement string fails with `cannot insert multiple commands into a prepared statement`. Anything other than a SELECT is a syntax error. A data-modifying CTE is refused. One `FETCH` of `row_cap + 1` rows enforces the cap. A plain cursor with no parameters uses the simple protocol and would run several statements. Calling `set_config('statement_timeout', '0', true)` inside the query doesn't stop the timer that is already running. With no parameters, `%` and `{}` in the agent's SQL pass through untouched.
- **How values are canonicalized and hashed.** Values go through `mulligan_audit.canon_row()`, which is `to_jsonb(row)` inside a function pinned to `TimeZone=UTC`, `DateStyle=ISO`, `IntervalStyle=iso_8601`, `extra_float_digits=1`, `bytea_output=hex` and `lc_monetary=C`. `field_hash()` is the sha256 hex of the field's jsonb text.
  - NULL: a NULL column hashes `null`. An absent row or field (deleted row, dropped table) hashes the empty string.
  - Numbers: numerics keep their stored scale, so `numeric(12,4)` 1.5 is `1.5000` and unconstrained numerics keep the input's scale. Floats use the shortest form that round-trips (`0.30000000000000004`), with `"NaN"` and `"Infinity"` as strings.
  - Times: timestamptz renders as `"2026-03-08T09:59:59.123456+00:00"`, timestamp has no offset, and interval renders as `"P1Y2M3DT4H5M6.789S"`.
  - jsonb uses jsonb's own normalized text: keys in order, duplicate keys dropped.
  - `extra_float_digits=0` makes `to_jsonb` emit `0.3`, which is why it's pinned. Writes made under `America/St_Johns` with `extra_float_digits=0`, and undone under `Asia/Kathmandu` with `extra_float_digits=3`, still match.
- **40 of 42 column types round-trip exactly** through `canon_row` → `jsonb_populate_record`: integers, numeric, float4/float8 (including NaN, ±Infinity and 5e-324), text with tabs, quotes, backslashes and astral characters, `char(n)` padding, date and timestamp (including infinity), time/timetz, interval, bytea, uuid, inet/cidr/macaddr, enum, ranges, 1-D and 2-D arrays, jsonb, xml, money, varbit, tsvector, point, composite and domain types. Two are lossy. `json` columns come back normalized (key order, whitespace, duplicate keys). A JSON `null` in a jsonb column comes back as SQL NULL. Use jsonb in the demo schema.
- **Hashes are computed in SQL, never in Python,** because jsonb's text spacing differs from `json.dumps`. psycopg's default jsonb loader turns every number into a float, so undo never sends values through Python. Each undo statement reads its audit row by id and converts it back with `jsonb_populate_record`.
- **psycopg needs ISO DateStyle.** Its C timestamptz loader fails on non-ISO output (`NotImplementedError: can't parse timestamptz with DateStyle 'SQL, DMY'`), so keep DateStyle ISO on gateway connections.
- **`after_hashes` keys.** Columns are `postgres:<table>/<pk>/<column>`. The table is a bare name in `public` and `schema.table` elsewhere. A composite PK is joined with `,`, and `%`, `/` and `,` are percent-escaped in every segment. INSERT hashes every column and UPDATE hashes the changed columns. DELETE adds a row key `postgres:<table>/<pk>` and a drop adds `postgres:<table>`, both with the absent hash. Generated columns are skipped. Hashes recomputed from the live DB just before each undo matched for all 9 actions.
- **Write guards.**
  - The table must have a PK.
  - A write is refused if the table, or any table an FK action can reach from it (CASCADE, SET NULL or SET DEFAULT, followed recursively), has no enabled audit trigger.
  - An empty filter needs `all: true`.
  - Table and column names are checked against the catalog.
  - Values go in as one jsonb parameter.
- **Roles.** `mulligan_gw` must own the app tables, because the soft drop needs ownership, and it needs CREATE on `public` and `mulligan_trash`. `enable_schema()` gives `mulligan_ro` SELECT on tables, but views need their own grant. The setup file must run as a superuser.
- **Not covered.** TRUNCATE (the tools never issue it), customer BEFORE triggers that rewrite restored values (for example `updated_at = now()`), hypertables, partitioned tables, and undoing an `ON UPDATE CASCADE`.
