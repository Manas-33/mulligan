# infra/ — Stream A

- `db/rewind-db/`: init SQL for Rewind's own log (TimescaleDB)
- `db/prod-db/`: init SQL for the demo company's "production" Postgres, including the audit triggers
- `deploy/`: deploy scripts for Vultr and the domain/TLS setup
