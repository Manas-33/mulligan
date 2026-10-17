-- Mulligan's footprint inside the demo company's database. Runs once, on an empty volume.
--
-- mulligan_trash: soft-dropped tables (pg_drop_table moves them here; undo moves them back).
-- mulligan_audit: the audit log the triggers write (added with the Postgres adapter).
-- mulligan_reader: the login role pg_query uses. SELECT on the company's tables only; it can't
--   see either schema above, and every pg_query also runs READ ONLY with a statement_timeout.

CREATE SCHEMA IF NOT EXISTS mulligan_trash;
CREATE SCHEMA IF NOT EXISTS mulligan_audit;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mulligan_reader') THEN
    CREATE ROLE mulligan_reader LOGIN PASSWORD 'mulligan_reader';
  END IF;
END
$$;

REVOKE ALL ON SCHEMA mulligan_trash FROM PUBLIC;
REVOKE ALL ON SCHEMA mulligan_audit FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT CONNECT ON DATABASE acme TO mulligan_reader;
GRANT USAGE ON SCHEMA public TO mulligan_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mulligan_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mulligan_reader;
ALTER ROLE mulligan_reader SET default_transaction_read_only = on;
ALTER ROLE mulligan_reader SET statement_timeout = '5s';
