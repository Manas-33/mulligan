"""Spike 0.3 runner: typed tools, audit trigger, undo, soft drop, pg_query limits.

Needs SPIKE03_ADMIN_DSN, a superuser DSN for a throwaway TimescaleDB container. It
recreates the database `spike03`, sets random role passwords (never printed), runs every
check and exits 1 if any check fails.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import time
from pathlib import Path

import psycopg
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

import pgtools as pg

HERE = Path(__file__).parent
DB = "spike03"
RESULTS: list[tuple[str, str, bool, str]] = []


def check(cid: str, title: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((cid, title, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {cid} {title}" + (f": {detail}" if detail else ""))
    return bool(ok)


def info(label: str, value) -> None:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    print(f"    {label}: {text}")


def err(e: BaseException) -> str:
    if isinstance(e, psycopg.Error) and e.diag.message_primary:
        return f"{type(e).__name__} [{e.sqlstate}]: {e.diag.message_primary}"
    return f"{type(e).__name__}: {e}"


# Setup ---------------------------------------------------------------------------------


def setup():
    admin_dsn = os.environ["SPIKE03_ADMIN_DSN"]
    with psycopg.connect(admin_dsn, autocommit=True) as a:
        a.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB)))
        a.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    admin = pg.connect(make_conninfo(admin_dsn, dbname=DB))
    admin.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    setup_sql = (HERE / "mulligan_prod_setup.sql").read_text()
    admin.execute(setup_sql)
    admin.execute(setup_sql)  # must be safe to re-run
    pw = {r: secrets.token_urlsafe(24) for r in ("mulligan_gw", "mulligan_ro")}
    for role, p in pw.items():
        admin.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(p)))
    gw = pg.connect(make_conninfo(admin_dsn, dbname=DB, user="mulligan_gw", password=pw["mulligan_gw"]))
    ro = pg.connect(make_conninfo(admin_dsn, dbname=DB, user="mulligan_ro", password=pw["mulligan_ro"]))
    gw.execute((HERE / "seed.sql").read_text())
    skipped = admin.execute("SELECT t::text FROM mulligan_audit.enable_schema('public') AS t").fetchall()
    admin.execute("GRANT SELECT ON legacy_pricing_v TO mulligan_ro")  # views need their own grant
    v = admin.execute(
        "SELECT current_setting('server_version') AS pg,"
        " (SELECT extversion FROM pg_extension WHERE extname = 'timescaledb') AS tsdb"
    ).fetchone()
    print(f"Postgres {v['pg']}, TimescaleDB {v['tsdb']}, psycopg {psycopg.__version__}")
    info("tables skipped by enable_schema (no PK)", [r["t"] for r in skipped])
    return admin, gw, ro


# Helpers -------------------------------------------------------------------------------


def naive_undo(gw, r: pg.ActionResult) -> str:
    """Replay the inverse in plain reverse audit order with no FK sort. Always rolled back."""
    try:
        with gw.transaction():
            gw.execute("SELECT set_config('mulligan.action_id', 'naive_probe', true)")
            for a in reversed(r.audit):
                pg.revert_row(gw, pg.load_table(gw, a["table_oid"]), a)
            raise psycopg.Rollback()
    except psycopg.Error as e:
        return f"fails: {err(e)}"
    return "works"


def audit_order(r: pg.ActionResult) -> list[str]:
    """table:op per audit row, '*' on rows the statement touched directly."""
    direct = set(r.direct_audit_ids)
    return [f"{a['table_name']}:{a['op'][0]}{'*' if a['id'] in direct else ''}" for a in r.audit]


def raises(fn, *exc):
    try:
        fn()
    except exc as e:
        return e
    return None


# Checks --------------------------------------------------------------------------------


def check_guards(admin, gw):
    print("\n== Typed tool guards")
    e = raises(lambda: pg.pg_delete_rows(gw, "customers", []), pg.ToolError)
    check("G1", "empty filter without all=true is refused", e is not None, str(e))
    e = raises(lambda: pg.pg_delete_rows(gw, "customers; DROP TABLE orders", [], all=True), pg.ToolError)
    check("G2", "table name is looked up in the catalog, not spliced", e is not None, str(e))
    e = raises(lambda: pg.pg_update_rows(gw, "customers", [{"column": "id) OR (1=1", "op": "=", "value": 1}], {"name": "x"}), pg.ToolError)
    check("G3", "filter column must exist", e is not None, str(e))
    r = pg.pg_update_rows(gw, "customers", [{"column": "email", "op": "=", "value": "x' OR '1'='1"}], {"name": "pwned"})
    check("G4", "filter values are bound parameters", r.rowcount == 0 and not r.audit, f"rowcount={r.rowcount}")
    e = raises(lambda: pg.pg_insert_rows(gw, "customers", [{"id": 99, "email": "x@x", "name": "x", "created_at": "2026-01-01T00:00:00Z"}]), psycopg.Error)
    check("G5", "agent can't pick a GENERATED ALWAYS id", isinstance(e, errors.GeneratedAlways), err(e) if e else "")
    gw.execute("ALTER TABLE support_tickets DISABLE TRIGGER mulligan_audit_row")
    try:
        e = raises(lambda: pg.pg_delete_rows(gw, "customers", [{"column": "id", "op": "=", "value": 3}]), pg.ToolError)
    finally:
        gw.execute("ALTER TABLE support_tickets ENABLE TRIGGER mulligan_audit_row")
    check("G6", "delete refused when a cascade target has no audit trigger", e is not None, str(e))
    row = gw.execute("SELECT quote_nullable(current_setting('mulligan.action_id', true)) AS v").fetchone()
    info("mulligan.action_id after an action txn ends (same session)", row["v"])


def check_trigger_order(admin, gw):
    """Why the FK sort: audit order depends on trigger names. Triggers on one table fire in
    name order, and FK cascade triggers are named RI_ConstraintTrigger_*."""
    print("\n== Audit order vs trigger name (scratch schema)")
    admin.execute("CREATE SCHEMA probe AUTHORIZATION mulligan_gw")
    gw.execute(
        """
        CREATE TABLE probe.p (id int PRIMARY KEY);
        CREATE TABLE probe.c (id int PRIMARY KEY, p_id int NOT NULL REFERENCES probe.p ON DELETE CASCADE);
        CREATE TABLE probe.gc (id int PRIMARY KEY, c_id int NOT NULL REFERENCES probe.c ON DELETE CASCADE);
        INSERT INTO probe.p VALUES (1); INSERT INTO probe.c VALUES (1, 1), (2, 1); INSERT INTO probe.gc VALUES (1, 1), (2, 2);
        """
    )
    out = {}
    for label, tgname in (("trigger 'mulligan_audit_row' (name sorts after RI_*)", "mulligan_audit_row"),
                          ("trigger 'A_audit' (name sorts before RI_*)", "A_audit")):
        for t in ("p", "c", "gc"):
            gw.execute(sql.SQL("DROP TRIGGER IF EXISTS mulligan_audit_row ON {t}; DROP TRIGGER IF EXISTS \"A_audit\" ON {t};").format(t=sql.Identifier("probe", t)))
            admin.execute(
                sql.SQL("CREATE TRIGGER {n} AFTER INSERT OR UPDATE OR DELETE ON {t} FOR EACH ROW EXECUTE FUNCTION mulligan_audit.capture('id')").format(
                    n=sql.Identifier(tgname), t=sql.Identifier("probe", t)
                )
            )
        aid = pg.new_action_id()
        with gw.transaction():
            gw.execute("SELECT set_config('mulligan.action_id', %s, true)", [aid])
            gw.execute("DELETE FROM probe.p WHERE id = 1")
        r = pg.ActionResult(aid, "probe", 1, pg.fetch_audit(gw, aid), {}, {"kind": "pg_audit"})
        naive = naive_undo(gw, r)
        sorted_undo = pg.undo_action(gw, r, undo_id="undo_probe")
        out[label] = {"audit order": audit_order(r), "plain reverse order": naive, "FK-sorted undo": sorted_undo["table_order"]}
        info(label, out[label])
    check(
        "E1",
        "cascade is logged parent first whatever the trigger name, so plain reverse order fails and the FK-sorted undo works",
        all(v["audit order"][0] == "p:D" and v["plain reverse order"].startswith("fails") and v["FK-sorted undo"] == ["p", "c", "gc"] for v in out.values()),
    )
    admin.execute("DROP SCHEMA probe CASCADE")


def check_hashes(gw, r: pg.ActionResult, label: str) -> bool:
    cur = pg.current_hashes(gw, r)
    bad = [k for k in r.after_hashes if cur.get(k) != r.after_hashes[k]]
    return check(f"H.{label}", f"after_hashes match the live DB before undo ({len(r.after_hashes)} keys)", not bad, f"mismatched={bad[:3]}")


def check_pg_query(admin, ro):
    print("\n== pg_query (mulligan_ro, READ ONLY, statement_timeout)")
    t0 = time.monotonic()
    e = raises(lambda: pg.pg_query(ro, "SELECT pg_sleep(10)"), psycopg.Error)
    dt = time.monotonic() - t0
    c4 = check("C4", "runaway pg_query is killed by statement_timeout", isinstance(e, errors.QueryCanceled) and dt < 6.5, f"{err(e) if e else 'not killed'} after {dt:.2f}s")
    t0 = time.monotonic()
    e = raises(lambda: pg.pg_query(ro, "SELECT set_config('statement_timeout', '0', true), pg_sleep(10)"), psycopg.Error)
    dt = time.monotonic() - t0
    check("Q1", "resetting statement_timeout inside the query doesn't disarm it", isinstance(e, errors.QueryCanceled) and dt < 6.5, f"{err(e) if e else 'not killed'} after {dt:.2f}s")
    e = raises(lambda: pg.pg_query(ro, "SELECT 1; SET statement_timeout = 0; SELECT pg_sleep(10)"), psycopg.Error)
    check("Q2", "multiple statements are rejected", e is not None, err(e) if e else "")
    for i, q in enumerate([
        "DELETE FROM customers",
        "WITH d AS (DELETE FROM customers RETURNING *) SELECT * FROM d",
        "SELECT nextval('products_id_seq')",
        "SELECT * FROM mulligan_audit.audit_log",
        "SELECT * FROM mulligan_trash.anything",
    ], 3):
        e = raises(lambda q=q: pg.pg_query(ro, q), psycopg.Error)
        check(f"Q{i}", f"refused: {q}", e is not None, err(e) if e else "ran!")
    r = pg.pg_query(ro, "SELECT name FROM customers WHERE email LIKE 'c2%' AND name <> '{x}'")
    check("Q8", "'%' and '{}' in agent SQL are passed through untouched", r["rows"] == [{"name": "Customer 2"}], json.dumps(r["rows"]))
    t0 = time.monotonic()
    e = raises(lambda: pg.pg_query(ro, "SELECT pg_sleep(3)", timeout_ms=1000), psycopg.Error)
    dt = time.monotonic() - t0
    check("Q9", "the per-transaction timeout is the one that fires (1 s, below the role's 5 s backstop)", isinstance(e, errors.QueryCanceled) and dt < 1.5, f"after {dt:.2f}s")
    r = pg.pg_query(ro, "SELECT g FROM generate_series(1, 5000) AS g", row_cap=1000)
    check("Q10", "row cap: only row_cap + 1 rows fetched, truncated flag set", len(r["rows"]) == 1000 and r["truncated"])
    return c4


def check_drop(admin, gw, ro, r: pg.ActionResult, before: dict) -> bool:
    p = r.inverse["payload"]
    oid = p["table_oid"]
    now = pg.table_structure(admin, oid)
    info("inverse payload", r.inverse)
    moved_idx = all(i[1] == "mulligan_trash" for i in now["indexes"])
    moved_seq = all(s[1] == "mulligan_trash" for s in now["sequences"])
    same = (
        now["name"][0][:2] == ("mulligan_trash", p["trash_name"])
        and now["columns"] == before["columns"]
        and len(now["indexes"]) == len(before["indexes"])
        and [s[2:] for s in now["sequences"]] == [s[2:] for s in before["sequences"]]
        and now["triggers"] == before["triggers"]
        and [f[0:2] for f in now["inbound_fks"]] == [f[0:2] for f in before["inbound_fks"]]
        and now["name"][0][2:] == before["name"][0][2:]
    )
    check("E5", "SET SCHEMA carried PK, indexes, identity sequence, triggers, inbound FKs, grants", same and moved_idx and moved_seq,
          f"indexes={[i[0] for i in now['indexes']]} seq={[s[0] for s in now['sequences']]}")
    privs = admin.execute(
        "SELECT has_schema_privilege('mulligan_ro', 'mulligan_trash', 'USAGE') AS trash,"
        " has_schema_privilege('mulligan_ro', 'mulligan_audit', 'USAGE') AS audit"
    ).fetchone()
    e1 = raises(lambda: pg.pg_query(ro, sql.SQL("SELECT * FROM {}").format(sql.Identifier(p["trash_schema"], p["trash_name"])).as_string(ro)), psycopg.Error)
    e2 = raises(lambda: pg.pg_query(ro, f"SELECT * FROM {p['name']}"), psycopg.Error)
    c3 = check(
        "C3",
        "read-only role can't see mulligan_trash (or mulligan_audit)",
        not privs["trash"] and not privs["audit"] and isinstance(e1, errors.InsufficientPrivilege) and isinstance(e2, errors.UndefinedTable),
        f"USAGE trash={privs['trash']} audit={privs['audit']}; select trashed: {err(e1) if e1 else 'ran!'}; select old name: {err(e2) if e2 else 'ran!'}",
    )
    names = pg.pg_query(ro, "SELECT relname FROM pg_class WHERE relnamespace = 'mulligan_trash'::regnamespace AND relkind = 'r'")
    info_schema = pg.pg_query(ro, "SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'mulligan_trash'")
    info("RO can still list trashed table names via pg_class (not data)", [x["relname"] for x in names["rows"]])
    info("RO information_schema.tables rows for mulligan_trash", info_schema["rows"][0]["n"])
    if p["name"] == "legacy_pricing":
        leak = pg.pg_query(ro, "SELECT count(*) AS n FROM legacy_pricing_v")
        info("RO reads the trashed table through an existing view legacy_pricing_v (rows)", leak["rows"][0]["n"])
        try:
            with gw.transaction():
                gw.execute("INSERT INTO price_overrides (legacy_pricing_id, reason) VALUES (1, 'while trashed')")
                raise psycopg.Rollback()
            info("insert into child whose FK points at the trashed table", "works: the FK check reads the trashed table")
        except psycopg.Error as e:
            info("insert into child whose FK points at the trashed table", err(e))
    return c3


def check_identifiers(admin, gw, r: pg.ActionResult):
    p = r.inverse["payload"]
    naive = f"{p['name']}__{r.action_id}"
    with gw.transaction():
        gw.execute(sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier("public", naive)))
        got = gw.execute("SELECT relname FROM pg_class WHERE relname LIKE %s", [p["name"] + "__act%"]).fetchone()["relname"]
        mb = "préférences_historique_ünïcödé_" * 2
        gw.execute(sql.SQL("CREATE TABLE {} (id int)").format(sql.Identifier("public", mb)))
        got_mb = gw.execute("SELECT relname FROM pg_class WHERE relname LIKE 'préférences%%'").fetchone()["relname"]
        raise psycopg.Rollback()
    ok = len(p["trash_name"].encode()) <= 63 and p["trash_name"].startswith(r.action_id.lower()) and all(len(x["trash_name"].encode()) <= 63 for x in p["renamed"])
    check("E4", "trash names fit in 63 bytes and keep the whole action id", ok, f"trash_name={p['trash_name']!r} ({len(p['trash_name'].encode())} bytes)")
    info(f"naive '<table>__<action_id>' ({len(naive.encode())} bytes) silently became", f"{got!r} ({len(got.encode())} bytes)")
    info(f"multibyte name ({len(mb.encode())} bytes) became", f"{got_mb!r} ({len(got_mb.encode())} bytes, valid UTF-8)")
    fitted = pg.fit_ident("act_01abc__" + "é" * 40)
    info("fit_ident on multibyte", f"{len(fitted.encode())} bytes, ends {fitted[-3:]!r}")


def check_double_drop(admin, gw):
    """Drop a table, recreate it under the same name, drop again: trash must not clash."""
    print("\n== Same table name dropped twice (scratch schema)")
    admin.execute("CREATE SCHEMA probe2 AUTHORIZATION mulligan_gw")
    ddl = "CREATE TABLE probe2.dup (id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY, v text UNIQUE); CREATE INDEX dup_v_idx ON probe2.dup (v)"
    gw.execute(ddl)
    with gw.transaction():  # naive: rename the table only, then move it
        gw.execute("ALTER TABLE probe2.dup RENAME TO naive1; ALTER TABLE probe2.naive1 SET SCHEMA mulligan_trash")
    gw.execute(ddl)
    e = raises(lambda: gw.execute("ALTER TABLE probe2.dup RENAME TO naive2; ALTER TABLE probe2.naive2 SET SCHEMA mulligan_trash"), psycopg.Error)
    info("naive soft drop, second time", err(e) if e else "worked")
    gw.execute("DROP TABLE mulligan_trash.naive1; DROP TABLE probe2.dup")
    old = pg.ALLOWED_SCHEMAS
    pg.ALLOWED_SCHEMAS = ("public", "probe2")
    try:
        gw.execute(ddl)
        d1 = pg.pg_drop_table(gw, "probe2.dup")
        gw.execute(ddl)
        d2 = pg.pg_drop_table(gw, "probe2.dup")
        pg.undo_action(gw, d2, undo_id="undo_d2")
        conflict = raises(lambda: pg.undo_action(gw, d1, undo_id="undo_d1"), pg.UndoError)
    finally:
        pg.ALLOWED_SCHEMAS = old
    check("E8", "soft drop renames indexes/sequences, so a second drop of the same name works", isinstance(e, psycopg.Error) and d2 is not None, f"renamed={[x['trash_name'] for x in d2.inverse['payload']['renamed']]}")
    check("E9", "undo of the older drop is a conflict when the name was reused", conflict is not None, str(conflict))
    admin.execute("DROP SCHEMA probe2 CASCADE")
    admin.execute("DO $$ DECLARE r record; BEGIN FOR r IN SELECT relname FROM pg_class WHERE relnamespace = 'mulligan_trash'::regnamespace AND relkind = 'r' AND relname LIKE '%__dup' LOOP EXECUTE format('DROP TABLE mulligan_trash.%I', r.relname); END LOOP; END $$")


def check_identity_sql(gw, r: pg.ActionResult):
    a = next(x for x in r.audit if x["table_name"] == "customers" and x["op"] == "DELETE")
    t = pg.load_table(gw, a["table_oid"])
    cols = sql.SQL(", ").join(map(sql.Identifier, t.writable))
    rc = sql.SQL(", ").join(sql.Identifier("r", c) for c in t.writable)
    plain = sql.SQL(
        "INSERT INTO {t} ({c}) SELECT {rc} FROM mulligan_audit.audit_log AS a"
        " CROSS JOIN LATERAL jsonb_populate_record(NULL::{t}, a.old_row) AS r WHERE a.id = %s"
    ).format(t=t.ident, c=cols, rc=rc)
    try:
        with gw.transaction():
            gw.execute(plain, [a["id"]])
            raise psycopg.Rollback()
        msg = "worked"
    except psycopg.Error as e:
        msg = err(e)
    info("re-insert into GENERATED ALWAYS identity without OVERRIDING SYSTEM VALUE", msg)


TYPES_DDL = """
CREATE TYPE probe3.mood AS ENUM ('ok', 'meh');
CREATE TYPE probe3.pair AS (a int, b text);
CREATE DOMAIN probe3.pos AS int CHECK (VALUE > 0);
CREATE TABLE probe3.types (
  id int PRIMARY KEY, i2 smallint, i8 bigint, n numeric, n_scaled numeric(12,4), f4 real, f8 double precision,
  f8_nan double precision, f8_ninf double precision, f8_tiny double precision, b boolean, t text, vc varchar(5),
  ch char(5), d date, d_inf date, ts timestamp, tstz timestamptz, tstz_inf timestamptz, tm time, tmtz timetz,
  iv interval, bin bytea, u uuid, ip inet, net cidr, mac macaddr, e probe3.mood, r int4range, tsr tstzrange,
  arr int[], arr2 text[][], jb jsonb, jb_null jsonb, js json, x xml, m money, bits varbit, tsv tsvector,
  pt point, comp probe3.pair, dom probe3.pos);
INSERT INTO probe3.types VALUES (1, -32768, 9223372036854775807, 12345678901234567890.123456789000, 1.5, 0.1,
  0.1::float8 + 0.2::float8, 'NaN', '-Infinity', 5e-324, true, E'tab\\there \"q\" \\\\ \u2713 \U0001D11E', 'abc', 'ab',
  '2024-02-29', 'infinity', '2026-10-17 01:02:03.456789', '2026-03-08 01:59:59.123456-08', '-infinity',
  '23:59:59.999999', '12:00:00+05:30', '1 year 2 mons 3 days 04:05:06.789', '\\x00ff10',
  'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', '192.168.0.1/24', '10.0.0.0/8', '08:00:2b:01:02:03', 'meh', '[1,10)',
  '[2026-01-01 00:00+00,2026-02-01 00:00+00)', '{1,NULL,3}', '{{a,b},{c,d}}', '{"b": 1, "a": [1.10, null], "a": 2}',
  'null', '{"b": 1,   "a": 2, "a": 3}', '<a x="1">t</a>', 1234.56, B'10110', 'fat:2 cat:1', '(1.5,-2)', ROW(1, 'x'), 7);
"""
KNOWN_LOSSY = {"js", "jb_null"}


def check_type_roundtrip(admin, gw):
    """Every column type through canon_row() and back with jsonb_populate_record(), which
    is exactly what capture + undo do. Run under hostile client settings."""
    print("\n== Canonical jsonb round-trip per column type (scratch schema)")
    admin.execute("CREATE SCHEMA probe3 AUTHORIZATION mulligan_gw")
    gw.execute(TYPES_DDL)
    oid = gw.execute("SELECT 'probe3.types'::regclass::oid AS o").fetchone()["o"]
    t = pg.load_table(gw, oid)
    pairs = sql.SQL(", ").join(
        sql.SQL("{o}::text AS {a}, {r}::text AS {b}, (mulligan_audit.canon_row(src) -> {c})::text AS {j}").format(
            o=sql.Identifier("src", c), r=sql.Identifier("back", c), a=sql.Identifier(c + ":a"),
            b=sql.Identifier(c + ":b"), c=sql.Literal(c), j=sql.Identifier(c + ":j"))
        for c in t.columns)
    row = gw.execute(sql.SQL(
        "SELECT {p} FROM probe3.types AS src CROSS JOIN LATERAL jsonb_populate_record(NULL::probe3.types, mulligan_audit.canon_row(src)) AS back"
    ).format(p=pairs)).fetchone()
    lossy = {}
    for c in t.columns:
        if row[c + ":a"] != row[c + ":b"]:
            lossy[c] = f"{row[c + ':a']!r} -> {row[c + ':b']!r}"
    canon = {f"{c} ({t.columns[c].type})": row[c + ":j"] for c in t.columns}
    info("canonical form per type", canon)
    info("columns that don't round-trip", lossy)
    check("E10", f"{len(t.columns) - len(lossy)}/{len(t.columns)} column types round-trip exactly; only json and jsonb JSON-null are lossy",
          set(lossy) == KNOWN_LOSSY, f"lossy={sorted(lossy)}")
    admin.execute("DROP SCHEMA probe3 CASCADE")


# Main ----------------------------------------------------------------------------------


def main() -> int:
    admin, gw, ro = setup()
    check_guards(admin, gw)
    check_trigger_order(admin, gw)

    # Hostile client settings for the writes; different ones for the undo below. Capture,
    # hashes and checksums must not depend on either. DateStyle output stays ISO because
    # psycopg's timestamptz loader can't parse anything else.
    gw.execute("SET TimeZone = 'America/St_Johns'; SET extra_float_digits = 0; SET DateStyle = 'ISO, DMY';"
               " SET IntervalStyle = 'sql_standard'; SET bytea_output = 'escape'")
    check_type_roundtrip(admin, gw)

    print("\n== Session: typed writes")
    start = pg.checksums(gw)
    actions: list[tuple[str, pg.ActionResult, dict, dict | None]] = []

    def act(label, fn, *a, **k):
        before = pg.checksums(gw)
        r = fn(gw, *a, **k)
        actions.append((label, r, before, None))
        info(f"{label} {r.tool} {r.action_id}", {"rowcount": r.rowcount, "direct": len(r.direct_audit_ids), "audit": audit_order(r)})
        return r

    a1 = act("A1", pg.pg_insert_rows, "products", [
        {"sku": "P-100", "name": "Gizmo ✓", "price": 12.5, "weight_kg": 0.30000000000000004, "tags": ["new", "a,b"],
         "attrs": {"b": 1, "a": [1.10, None]}, "updated_at": "2026-10-17T08:00:00.5"},
        {"sku": "P-101", "name": "O'Brien \"quoted\"", "price": "99999999.99", "weight_kg": None, "tags": None, "attrs": None, "updated_at": None},
    ])
    act("A2", pg.pg_update_rows, "products", [{"column": "sku", "op": "in", "value": ["P-001", "P-003", "P-100"]}], {"price": 9.99, "weight_kg": 2.5})
    a3 = act("A3", pg.pg_delete_rows, "customers", [{"column": "email", "op": "=", "value": "c1@acme.test"}])
    a4 = act("A4", pg.pg_delete_rows, "employees", [{"column": "id", "op": "=", "value": 10}])
    act("A5", pg.pg_delete_rows, "customers", [{"column": "active", "op": "=", "value": False}])
    act("A6", pg.pg_insert_rows, "orders", [{"customer_id": 2, "placed_at": "2026-10-17T10:00:00-07:00", "note": None}])
    act("A7", pg.pg_update_rows, "order_items", [{"column": "order_id", "op": "=", "value": 3}], {"qty": 9})
    drop_before = pg.table_structure(admin, pg.resolve_table(gw, "legacy_pricing").oid)
    a8 = act("A8", pg.pg_drop_table, "legacy_pricing")
    long_before = pg.table_structure(admin, pg.resolve_table(gw, "quarterly_revenue_snapshots_by_region_and_sales_channel_v2").oid)
    a9 = act("A9", pg.pg_drop_table, "quarterly_revenue_snapshots_by_region_and_sales_channel_v2")

    cascade_tables = {(a["table_name"], a["op"]) for a in a3.audit}
    info("A3 cascade audit rows by table", sorted(f"{t}:{o}" for t, o in cascade_tables))
    note_row = next(a for a in a3.audit if a["table_name"] == "customer_notes")
    info("example audit row (SET NULL cascade)", {k: note_row[k] for k in ("id", "action_id", "schema_name", "table_name", "op", "pk", "changed", "old_row", "new_row")})
    info("example after_hashes keys", list(a1.after_hashes)[:3] + [k for k in a3.after_hashes if "order_items" in k][:1] + list(a8.after_hashes))
    info("A3 plain reverse audit order", naive_undo(gw, a3))
    info("A4 plain reverse audit order", naive_undo(gw, a4))
    check_identity_sql(gw, a3)

    print("\n== Soft drop while trashed")
    c3 = check_drop(admin, gw, ro, a8, drop_before)
    check_identifiers(admin, gw, a9)

    print("\n== Undo, newest action first")
    gw.execute("SET TimeZone = 'Asia/Kathmandu'; SET extra_float_digits = 3; SET DateStyle = 'ISO, MDY';"
               " SET IntervalStyle = 'postgres_verbose'; SET bytea_output = 'hex'")
    per_action_ok = True
    for label, r, before, _ in reversed(actions):
        check_hashes(gw, r, label)
        res = pg.undo_action(gw, r, undo_id="undo_" + r.action_id)
        after = pg.checksums(gw)
        diff = sorted(k for k in before if k != "*" and before[k] != after.get(k))
        per_action_ok &= check(f"U.{label}", f"undo {r.tool}: checksum equals pre-action", before == after, f"order={res['table_order']} diff={diff}")
        if label == "A4":
            info("A4 re-insert order (employees ids)", [s.split(" ", 2)[2] for s in res["steps"]])
    end = pg.checksums(gw)
    c1 = check("C1", "session checksum after undo equals before (incl. customer delete cascading into orders, order_items, support_tickets, customer_notes)",
               end == start and per_action_ok and {"orders", "order_items", "support_tickets"} <= {t for t, o in cascade_tables if o == "DELETE"},
               f"start={start['*'][:16]} end={end['*'][:16]}")

    after_lp = pg.table_structure(admin, a8.inverse["payload"]["table_oid"])
    after_long = pg.table_structure(admin, a9.inverse["payload"]["table_oid"])
    fk = raises(lambda: gw.execute("INSERT INTO price_overrides (legacy_pricing_id, reason) VALUES (-1, 'x')"), psycopg.Error)
    ro_back = pg.pg_query(ro, "SELECT count(*) AS n FROM legacy_pricing")["rows"][0]["n"]
    c2 = check("C2", "dropped table is back with data, PK, indexes, identity sequence, triggers, grants and inbound FKs",
               after_lp == drop_before and after_long == long_before and isinstance(fk, errors.ForeignKeyViolation) and ro_back > 0,
               f"structure equal={after_lp == drop_before and after_long == long_before}; inbound FK enforced={isinstance(fk, errors.ForeignKeyViolation)}; RO rows={ro_back}")

    c4 = check_pg_query(admin, ro)
    check_double_drop(admin, gw)

    print("\n== Summary")
    for cid, title, ok, _ in sorted(r for r in RESULTS if r[0] in ("C1", "C2", "C3", "C4")):
        print(f"  {cid} {'PASS' if ok else 'FAIL'}  {title}")
    failed = [cid for cid, _, ok, _ in RESULTS if not ok]
    print(f"  {len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed" + (f"; failed: {failed}" if failed else ""))
    for c in (admin, gw, ro):
        c.close()
    return 1 if failed or not (c1 and c2 and c3 and c4) else 0


if __name__ == "__main__":
    sys.exit(main())
