# approvals-photon/ — Stream B (TypeScript)

A spectrum-ts service that sends iMessage approvals ("Reply YES 4821") and rewind summaries. It parses replies and forwards them to `POST /api/approvals/{action_id}/decision`.
It serves `POST /notify/approval` and `POST /notify/summary` (see `contracts/openapi.yaml`).
