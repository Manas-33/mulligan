# approvals-photon/ — Stream B (TypeScript)

A spectrum-ts service that sends iMessage approvals ("Reply YES 4821") and rewind summaries. It parses replies and forwards them to `POST /api/approvals/{action_id}/decision`.
It serves `POST /notify/approval` and `POST /notify/summary` (see `contracts/openapi.yaml`).

It also watches the gateway's rewind jobs (`GET /api/rewind`, every 2 s) and texts the approver a one-line summary when a run finishes, for example "Rewound round m7c4. 53 changes undone, 2 held actions refused and kept 2 human edits. Postgres, GitHub and Notion checksums match." Each job is texted once (the idempotency key is the job id, shared with `POST /notify/summary`), and only runs that finish after the service starts.

## Run

```bash
make photon-setup   # npm ci
make photon         # http://127.0.0.1:8100 (needs MULLIGAN_SERVICE_TOKEN, PHOTON_*, APPROVER_HANDLE in .env's environment)
make photon-check   # type-check + tests
```

Uses Photon 2.0 through `@photon-ai/api` (not spectrum-ts). The approver must be on iMessage and must text the shared line once first; see `docs/SDK_NOTES.md` (spike 0.6). Shared types come from `contracts/generated/types.ts` via `src/types.ts`.
