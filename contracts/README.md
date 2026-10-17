# contracts/ — SHARED (both approve changes)

The single source of truth that both streams build against.

- `action.schema.json`: Action, Session, RewindJob, request bodies, event envelopes and the Photon service bodies (JSON Schema 2020-12)
- `openapi.yaml`: gateway REST API under `/api`, plus `/healthz`
- `photon.openapi.yaml`: the Photon service's `/notify/*` API, which the gateway calls
- `events.md`: SSE and internal event names and payloads
- `mcp_tools.md`: the typed MCP tools agents see (no raw SQL writes), with input schemas
- `reversibility_policy.yaml`: the label for every built-in tool. It is the authority, and the rules gate never overrides it.
- `generated/types.ts`: TS types generated from the schema. Do not hand-edit. The Pydantic models are generated into `gateway/mulligan/models.py`.

```bash
make types           # regenerate both type sets after editing action.schema.json
make lint-contracts  # lint both OpenAPI specs; examples are checked against the schema
make mocks           # Prism: gateway API on http://127.0.0.1:4010, Photon service on :4011
```

CI fails if the generated types are out of date.
