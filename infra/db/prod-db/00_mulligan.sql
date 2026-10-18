-- Mulligan's footprint in the demo company's database. Runs once, as the superuser, on an empty
-- volume, before the seed (10_seed.sh) creates the app tables as mulligan_gw so it owns them.
-- 20_enable.sql then attaches the audit trigger to every table in public.
--
-- mulligan_gw: the gateway's role for the typed write tools. It owns the app tables, because the
--   soft drop (ALTER TABLE ... SET SCHEMA) needs ownership.
-- mulligan_ro: pg_query's role. SELECT on app tables only, no USAGE on mulligan_audit or
--   mulligan_trash, and read-only with a timeout even before pg_query sets its own.
-- mulligan_audit: the audit log the trigger writes. mulligan_trash: soft-dropped tables.
--
-- The passwords below are for this local container only (bound to 127.0.0.1).

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mulligan_gw') THEN
    CREATE ROLE mulligan_gw LOGIN PASSWORD 'mulligan_gw';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mulligan_ro') THEN
    CREATE ROLE mulligan_ro LOGIN PASSWORD 'mulligan_ro';
  END IF;
END $$;

ALTER ROLE mulligan_ro SET default_transaction_read_only = on;
ALTER ROLE mulligan_ro SET statement_timeout = '5s';
ALTER ROLE mulligan_ro SET idle_in_transaction_session_timeout = '10s';
GRANT CONNECT ON DATABASE acme TO mulligan_gw, mulligan_ro;

-- Schemas -----------------------------------------------------------------------------

CREATE SCHEMA IF NOT EXISTS mulligan_audit;
CREATE SCHEMA IF NOT EXISTS mulligan_trash;
REVOKE ALL ON SCHEMA mulligan_audit FROM PUBLIC;
REVOKE ALL ON SCHEMA mulligan_trash FROM PUBLIC;
GRANT USAGE ON SCHEMA mulligan_audit TO mulligan_gw;
-- CREATE on both schemas: SET SCHEMA needs CREATE on the target (trash on drop, public on undo).
GRANT USAGE, CREATE ON SCHEMA mulligan_trash TO mulligan_gw;
GRANT USAGE, CREATE ON SCHEMA public TO mulligan_gw;
GRANT USAGE ON SCHEMA public TO mulligan_ro;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE mulligan_gw IN SCHEMA public GRANT SELECT ON TABLES TO mulligan_ro;

-- Audit log ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mulligan_audit.audit_log (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- write order
  action_id   text        NOT NULL,                     -- current_setting('mulligan.action_id')
  txid        xid8        NOT NULL DEFAULT pg_current_xact_id(),
  logged_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
  table_oid   oid         NOT NULL,                     -- stable across SET SCHEMA and RENAME
  schema_name name        NOT NULL,
  table_name  name        NOT NULL,
  op          text        NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
  pk          jsonb       NOT NULL,                     -- {pk_col: value}; NEW for INSERT/UPDATE, OLD for DELETE
  old_row     jsonb,                                    -- canon_row(OLD); NULL for INSERT
  new_row     jsonb,                                    -- canon_row(NEW); NULL for DELETE
  changed     text[]                                    -- UPDATE only: columns whose value changed
);
CREATE INDEX IF NOT EXISTS audit_log_action_idx ON mulligan_audit.audit_log (action_id, id);
REVOKE ALL ON mulligan_audit.audit_log FROM PUBLIC;
GRANT SELECT ON mulligan_audit.audit_log TO mulligan_gw;

-- Canonical row form. Every hash and every audit row goes through this one function, so
-- capture time and conflict-check time render values the same way whatever the client's
-- session settings are: timestamptz in UTC, floats with shortest round-trip digits,
-- intervals in ISO 8601, bytea as hex, money in the C locale.
CREATE OR REPLACE FUNCTION mulligan_audit.canon_row(r anyelement) RETURNS jsonb
LANGUAGE sql STABLE
SET search_path = pg_catalog, pg_temp
SET TimeZone = 'UTC'
SET DateStyle = 'ISO, YMD'
SET IntervalStyle = 'iso_8601'
SET extra_float_digits = 1
SET bytea_output = 'hex'
SET lc_monetary = 'C'
AS $$ SELECT to_jsonb(r) $$;

-- sha256 of one field (or one whole row) in canonical form. SQL NULL (row or field absent)
-- hashes the empty string, which no jsonb value renders as, so "row deleted" is distinct from
-- "column is NULL" (which renders as 'null').
CREATE OR REPLACE FUNCTION mulligan_audit.field_hash(v jsonb) RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT encode(sha256(convert_to(coalesce(v::text, ''), 'UTF8')), 'hex') $$;

-- Generic row trigger. Logs only inside a Mulligan action: the gateway runs
--   SELECT set_config('mulligan.action_id', 'act_...', true)   -- same as SET LOCAL
-- at the start of each action's transaction. Writes made outside an action (humans,
-- migrations) are not logged. Trigger arguments are the table's primary-key columns.
-- Rows changed by ON DELETE CASCADE / SET NULL are logged too, after the parent row.
CREATE OR REPLACE FUNCTION mulligan_audit.capture() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  v_action  text := current_setting('mulligan.action_id', true);
  v_old     jsonb;
  v_new     jsonb;
  v_pk      jsonb;
  v_changed text[];
BEGIN
  -- NULL if never set in this session, '' after an earlier SET LOCAL has ended.
  IF v_action IS NULL OR v_action = '' THEN
    RETURN NULL;
  END IF;
  IF TG_OP <> 'INSERT' THEN v_old := mulligan_audit.canon_row(OLD); END IF;
  IF TG_OP <> 'DELETE' THEN v_new := mulligan_audit.canon_row(NEW); END IF;
  IF TG_OP = 'UPDATE' THEN
    SELECT array_agg(n.key ORDER BY n.key) INTO v_changed
      FROM jsonb_each(v_new) AS n
     WHERE v_old -> n.key IS DISTINCT FROM n.value;
    IF v_changed IS NULL THEN
      RETURN NULL;  -- no-op update, nothing to undo
    END IF;
  END IF;
  SELECT jsonb_object_agg(k, coalesce(v_new, v_old) -> k) INTO v_pk FROM unnest(TG_ARGV) AS k;
  INSERT INTO mulligan_audit.audit_log
    (action_id, table_oid, schema_name, table_name, op, pk, old_row, new_row, changed)
  VALUES
    (v_action, TG_RELID, TG_TABLE_SCHEMA, TG_TABLE_NAME, TG_OP, v_pk, v_old, v_new, v_changed);
  RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION mulligan_audit.capture() FROM PUBLIC;

-- Attach the audit trigger to one table. The table must have a primary key.
CREATE OR REPLACE FUNCTION mulligan_audit.attach(t regclass) RETURNS void
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  v_pk text[];
BEGIN
  SELECT array_agg(a.attname::text ORDER BY k.ord) INTO v_pk
    FROM pg_index AS i
   CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
    JOIN pg_attribute AS a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
   WHERE i.indrelid = t AND i.indisprimary;
  IF v_pk IS NULL THEN
    RAISE EXCEPTION 'mulligan: % has no primary key, so typed writes and undo cannot use it', t;
  END IF;
  EXECUTE format('DROP TRIGGER IF EXISTS mulligan_audit_row ON %s', t);
  EXECUTE format(
    'CREATE TRIGGER mulligan_audit_row AFTER INSERT OR UPDATE OR DELETE ON %s '
    'FOR EACH ROW EXECUTE FUNCTION mulligan_audit.capture(%s)',
    t, (SELECT string_agg(quote_literal(c), ', ') FROM unnest(v_pk) AS c));
END $$;

-- Attach the trigger to every table in a schema and give the read-only role SELECT on them
-- (and on the schema's views). Tables without a primary key are skipped and returned.
CREATE OR REPLACE FUNCTION mulligan_audit.enable_schema(s name) RETURNS SETOF regclass
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS t, c.relkind,
           EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary) AS has_pk
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname = s AND c.relkind IN ('r', 'p', 'v', 'm') AND NOT c.relispartition
  LOOP
    EXECUTE format('GRANT SELECT ON %s TO mulligan_ro', r.t);
    IF r.relkind NOT IN ('r', 'p') THEN
      CONTINUE;
    ELSIF r.has_pk THEN
      PERFORM mulligan_audit.attach(r.t);
    ELSE
      RETURN NEXT r.t;
    END IF;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION mulligan_audit.attach(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION mulligan_audit.enable_schema(name) FROM PUBLIC;
