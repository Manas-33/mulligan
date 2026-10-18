# 03 — Postgres typed-tool undo

Owner: Stream A. Spike 0.3 in PLAN.md Phase 0. Results are in `docs/SDK_NOTES.md`.

| File | What it is |
|---|---|
| `mulligan_prod_setup.sql` | Reusable: `mulligan_gw` and `mulligan_ro` roles, `mulligan_audit` (audit table, `canon_row`, `field_hash`, the generic `capture()` trigger, `attach`, `enable_schema`) and `mulligan_trash`. `infra/db/prod-db/` copies this in Phase 2. |
| `seed.sql` | Spike tables and data: a 3-level cascade, a `SET NULL` child, a self-referencing FK, identity and generated columns, a table with an inbound FK and a view, and a 58-byte table name. |
| `pgtools.py` | Typed tools (`pg_insert_rows`, `pg_update_rows`, `pg_delete_rows`, `pg_drop_table`, `pg_query`), `after_hashes`, FK-sorted undo, soft-drop undo, checksums. |
| `run_spike.py` | Runs every check and exits 1 on any failure. |
| `run_output.txt` | Output of the last run. |

Run it against a throwaway container (any free port except 5433/5434):

```bash
docker run -d --name spike03-pg -e POSTGRES_PASSWORD=<pw> -p 127.0.0.1:55433:5432 timescale/timescaledb:latest-pg16
uv sync
SPIKE03_ADMIN_DSN=postgresql://postgres:<pw>@127.0.0.1:55433/postgres uv run python run_spike.py
docker rm -f spike03-pg
```

The script recreates the `spike03` database on each run and gives the two roles random passwords that it never prints.
