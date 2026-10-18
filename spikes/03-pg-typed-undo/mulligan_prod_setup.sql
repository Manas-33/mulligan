-- Mulligan prod-db setup: audit trigger, mulligan_trash, gateway and read-only roles.
--
-- Run once per database as a superuser, before the app tables exist. Safe to re-run.
-- Passwords are not set here. The init script sets them from .env:
--   ALTER ROLE mulligan_gw LOGIN PASSWORD '...';   ALTER ROLE mulligan_ro LOGIN PASSWORD '...';
-- After the app tables are created (by mulligan_gw, so it owns them), run:
--   SELECT mulligan_audit.enable_schema('public');

-- Roles -------------------------------------------------------------------------------

DO $$
BEGIN
  -- Gateway role for the typed write tools. It must own the app tables, because the
  -- soft drop (ALTER TABLE ... SET SCHEMA) needs table ownership.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mulligan_gw') THEN
    CREATE ROLE mulligan_gw NOLOGIN;
  END IF;
  -- Role for pg_query. SELECT on app tables only, no USAGE on mulligan_audit or mulligan_trash.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mulligan_ro') THEN
    CREATE ROLE mulligan_ro NOLOGIN;
  END IF;
END $$;

-- Backstops. pg_query also sets these per transaction.
ALTER ROLE mulligan_ro SET default_transaction_read_only = on;
ALTER ROLE mulligan_ro SET statement_timeout = '5s';
ALTER ROLE mulligan_ro SET idle_in_transaction_session_timeout = '10s';

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
-- intervals in ISO 8601, bytea as hex, money in the C locale. jsonb's own text output is
-- setting-independent.
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

-- Hash of one field (or one whole row) in canonical form: sha256 of the jsonb text.
-- SQL NULL (field or row absent) hashes the empty string, which no jsonb value renders as,
-- so "row deleted" is distinct from "column is NULL" (which renders as 'null').
CREATE OR REPLACE FUNCTION mulligan_audit.field_hash(v jsonb) RETURNS text
LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, pg_temp
AS $$ SELECT encode(sha256(convert_to(coalesce(v::text, ''), 'UTF8')), 'hex') $$;

-- Generic row trigger. Logs only inside a Mulligan action: the gateway runs
--   SELECT set_config('mulligan.action_id', 'act_...', true)   -- same as SET LOCAL
-- at the start of each action's transaction. Writes made outside an action (humans,
-- migrations) are not logged. Trigger arguments are the table's primary-key columns.
-- Rows removed or changed by ON DELETE CASCADE / SET NULL are logged too, after the
-- parent row: FK actions queue their row events behind the outer statement's, so
-- audit order is parent first and pg_trigger_depth() is 1 for cascaded rows as well.
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
-- Re-run after changing a table's primary key.
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

-- Attach the trigger to every ordinary or partitioned table in a schema and give the
-- read-only role SELECT on them. Tables without a primary key are skipped and returned.
CREATE OR REPLACE FUNCTION mulligan_audit.enable_schema(s name) RETURNS SETOF regclass
LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
  r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS t,
           EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = c.oid AND i.indisprimary) AS has_pk
      FROM pg_class AS c
      JOIN pg_namespace AS n ON n.oid = c.relnamespace
     WHERE n.nspname = s AND c.relkind IN ('r', 'p') AND NOT c.relispartition
  LOOP
    EXECUTE format('GRANT SELECT ON %s TO mulligan_ro', r.t);
    IF r.has_pk THEN
      PERFORM mulligan_audit.attach(r.t);
    ELSE
      RETURN NEXT r.t;
    END IF;
  END LOOP;
END $$;
REVOKE ALL ON FUNCTION mulligan_audit.attach(regclass) FROM PUBLIC;
REVOKE ALL ON FUNCTION mulligan_audit.enable_schema(name) FROM PUBLIC;
