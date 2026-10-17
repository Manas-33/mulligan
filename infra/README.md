# infra/ — Stream A

- `db/mulligan-db/`: init SQL for Mulligan's own log (TimescaleDB)
- `db/prod-db/`: init SQL for the demo company's "production" Postgres: the audit triggers, the read-only role `pg_query` uses, and the `mulligan_trash` schema for soft-dropped tables
- `deploy/`: the free cloudflared quick tunnel (no account) that gives the gateway's **MCP port only** (`:8001`) a public HTTPS URL, which Devin needs: `cloudflared tunnel --url http://localhost:8001`. Never point it at `:8000`, since the REST API stays on `127.0.0.1`. A quick tunnel's URL changes when it restarts, so update Devin's MCP settings if it does.
