# contracts/ — SHARED (both approve changes)

The single source of truth that both streams build against. It gets filled in Phase 1.

- `action.schema.json`: Action, Session, RewindJob (JSON Schema)
- `openapi.yaml`: gateway REST API under `/api`, plus the Photon service `/notify/*`
- `events.md`: SSE and internal event names and payloads
- `mcp_tools.md`: the typed MCP tools agents see (no raw SQL writes), with input schemas
- `reversibility_policy.yaml`: the label for every built-in tool. It is the authority, and the classifier never overrides it.
- `generated/`: Pydantic and TS types generated from the schema. Do not hand-edit.
