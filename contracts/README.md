# contracts/ — SHARED (both approve changes)

The single source of truth that both streams build against. It gets filled in Phase 1.

- `action.schema.json`: Action, Session, RewindJob (JSON Schema)
- `openapi.yaml`: gateway REST API under `/api`, plus the Photon service `/notify/*`
- `events.md`: SSE and internal event names and payloads
- `mcp_tools.md`: the MCP tools agents see, with input schemas and default reversibility
- `generated/`: Pydantic and TS types generated from the schema. Do not hand-edit.
