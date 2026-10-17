# gateway/ — Python 3.12, FastAPI + official `mcp` SDK

The MCP server (Streamable HTTP) that agents connect to. It holds every real credential.

| Module | Owner | Purpose |
|---|---|---|
| `rewind/mcp/` | A | MCP server and tool registry, per-agent bearer auth |
| `rewind/api/` | A | REST `/api/*` and SSE `/api/events` |
| `rewind/log/` | A | Action log in TimescaleDB |
| `rewind/orchestrator/` | A | Rewind plan, reverse dependency order, verification |
| `rewind/adapters/{postgres,github,notion}/` | A | Capture before/after, execute, revert, checksum |
| `rewind/holds/` | B | Email and payments held channels, approval state machine |
| `rewind/classify/` | B | Reversibility classifier client with rules fallback |
| `migrations/` | A | rewind-db schema |
