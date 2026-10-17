# infra/ — Stream A

- `db/mulligan-db/`: init SQL for Mulligan's own log (TimescaleDB)
- `db/prod-db/`: init SQL for the demo company's "production" Postgres: the audit triggers, the read-only role `pg_query` uses, and the `mulligan_trash` schema for soft-dropped tables
- `deploy/`: deploy scripts for Vultr and the domain/TLS setup
