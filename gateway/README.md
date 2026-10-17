# gateway/ — Python 3.12, FastAPI + official `mcp` SDK

The MCP server (Streamable HTTP) that agents connect to. It holds every real credential.

| Module | Owner | Purpose |
|---|---|---|
| `mulligan/mcp_server/` (not `mcp/`: that would shadow the `mcp` SDK) | A | MCP server, typed tool registry (no raw SQL writes), upstream MCP pass-through, per-agent bearer auth |
| `mulligan/api/` | A | REST `/api/*` and SSE `/api/events` |
| `mulligan/log/` | A | Action log in TimescaleDB |
| `mulligan/orchestrator/` | A | Rewind plan (newest action first), verification against the expected state |
| `mulligan/adapters/{postgres,github,notion}/` | A | Capture before/after and field hashes, execute, revert (skipping fields a human changed), checksum |
| `mulligan/holds/` | B | Email and payments held channels, approval state machine |
| `mulligan/classify/` | B | Policy table lookup for built-in tools, and the rules gate for upstream tools (fails closed) |
| `migrations/` | A | mulligan-db schema |
