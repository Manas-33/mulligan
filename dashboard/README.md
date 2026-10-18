# dashboard/ — Stream B (sole owner of all UI)

React + Vite. Shows the session timeline, held actions, the rewind dialog (dry run, then run), live rewind progress, checksum proof, Sentry trace links, kept-human-edit flags, a "not undoable" marker on upstream calls and a "replay" badge on replay sessions.
It talks to the gateway only through `contracts/openapi.yaml` and the SSE events. It uses Prism mocks until the gateway is ready. The design is done in Paper.

## Run

```bash
make dashboard-setup   # npm ci
make mocks             # Prism: fake gateway API on :4010 (the dashboard's default)
make dashboard         # http://127.0.0.1:5173
make dashboard-check   # type-check + tests + build
```

Point it at the real gateway with `VITE_MULLIGAN_API_URL=http://127.0.0.1:8000` and `VITE_MULLIGAN_DASHBOARD_TOKEN=<MULLIGAN_DASHBOARD_TOKEN>`. Values are baked in at build time (fine for the laptop-only demo). Shared types come from `contracts/generated/types.ts` via `src/types.ts`.
