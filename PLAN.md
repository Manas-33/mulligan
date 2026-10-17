# Mulligan — Implementation Plan

Source: `MULLIGAN_IDEA.md`. No code until this plan is approved.

## How we work (applies to every phase)

- **Phase-gated.** Each phase ends with a **STOP — approval gate**. Nothing in phase N+1 starts until both people approve the phase N exit checklist.
- **Goal-driven.** Each phase opens with a `GOAL` block (objective + acceptance checks). Paste it as the phase goal at the start of each Claude Code session.
- **Loop technique, at least 3 iterations per phase, per workstream.** Run `/loop` (self-paced) on the phase goal. Each pass does one of these:
  1. **Build.** Check the docs for every SDK the phase touches, record pinned versions and gotchas in `docs/SDK_NOTES.md`, then implement.
  2. **Verify.** Run the unit and integration tests and the phase's acceptance script, then fix failures.
  3. **Audit.** Run `/code-review` on the diff, check it against `contracts/`, and look for secret leaks, missing Sentry spans, and missing tests. Then fix.
  Keep looping (verify, then audit) until every acceptance check passes twice in a row. Three passes is the minimum, not the target.
- **Docs before SDKs.** Nobody calls an SDK until they've checked its current docs. Versions get pinned in `pyproject.toml` / `package.json` and noted in `docs/SDK_NOTES.md`.
- **Safety.** Use test or sandbox accounts only (a throwaway GitHub org, a Notion workspace, Stripe test mode, a test iMessage line). Secrets live in `.env` (gitignored) and `.env.example` documents them. Agents never see a credential.
- **Git.** Each stream works on its own branch per phase (`a/p2-postgres-undo`, `b/p2-photon`, and so on) in its own worktree and opens PRs to `main`. Any change under `contracts/` needs a PR approved by both people.

## The two workstreams

| | **Stream A — Core and Undo** | **Stream B — Holds, Approvals, Rules Gate and all UI** |
|---|---|---|
| Owns | `gateway/` core (MCP server, action log, undo orchestrator), `gateway/mulligan/adapters/{postgres,github,notion}`, `demo/` (seed company, rogue agent live run and replay, mock upstream MCP server, reset, checksums), `fetch-agent/`, `infra/` | `dashboard/` (**all UI, sole owner**), `approvals-photon/` (TS), `gateway/mulligan/holds/` (email and payments adapters, approval state machine), `gateway/mulligan/classify/` (policy lookup and the rules gate for unknown upstream tools) |
| Sponsor surface | Fetch.ai, Devin, Sentry backend | Photon, Visa/Stripe, Paper, Sentry frontend (Session Replay), StepFun |

Stream B owns the UI outright, so Stream A ships no UI code. Stream A's only UI-facing outputs are the REST API and the SSE events in the contracts.

**Collision rules.** Stream B's code inside `gateway/` lives only in `holds/` and `classify/`. It plugs into core through the `Adapter`, `HoldManager`, `Policy` and `Classifier` interfaces in `contracts/`. Shared files (`gateway/mulligan/app.py`, `pyproject.toml`, `docker-compose.yml`) belong to Stream A. Stream B asks for changes to them in a PR.

## Repo structure (already in place on `main`)

Every folder has a README that states its owner and scope. Ownership and git rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

```
mulligan/
├── README.md  PLAN.md  CONTRIBUTING.md  MULLIGAN_IDEA.md
├── .env.example  .gitignore  .editorconfig
├── contracts/                 # SHARED. Changes need both people's approval
│   └── generated/             #   generated Pydantic + TS types (don't hand-edit)
├── gateway/                   # Python 3.12, FastAPI + official `mcp` SDK
│   ├── mulligan/
│   │   ├── mcp_server/        #   A: MCP server (Streamable HTTP), typed tool registry, upstream pass-through, agent auth
│   │   ├── api/               #   A: REST /api/* + SSE /api/events
│   │   ├── log/               #   A: action log (TimescaleDB)
│   │   ├── orchestrator/      #   A: rewind plan (newest action first), verify
│   │   ├── adapters/
│   │   │   ├── postgres/      #   A
│   │   │   ├── github/        #   A
│   │   │   └── notion/        #   A
│   │   ├── holds/             #   B: email + payments holds, approval state machine
│   │   └── classify/          #   B: policy lookup + rules gate for unknown upstream tools
│   ├── migrations/            #   A: mulligan-db schema
│   └── tests/
├── approvals-photon/          # B: TypeScript spectrum-ts service (src/, test/)
├── dashboard/                 # B: React + Vite. ALL UI (src/, public/)
├── fetch-agent/               # A: uAgents Guardian + worker (tests/)
├── demo/                      # A: seed/, scripts/ (rogue agent live + replay, teammate edit, reset), recordings/, upstream/, checksums/
├── infra/                     # A: db/mulligan-db/, db/prod-db/ (audit triggers, read-only role, mulligan_trash), deploy/ (cloudflared tunnel)
├── spikes/                    # Phase 0 throwaway scripts, one NN-name/ folder per check
├── docs/SDK_NOTES.md          # both: pinned versions + gotchas per SDK (append only)
└── .github/workflows/         # A: CI
```

To start: clone the repo, `cp .env.example .env`, fill in sandbox credentials only, then create your own branch and worktree (`a/p0-spikes` or `b/p0-spikes`).

---

## Phase 0 — De-risk spikes (parallel, throwaway code in `spikes/`)

```
GOAL: Prove every risky integration works end to end with a minimal script before we design around it.
Each spike yields: a pass/fail result, the exact SDK version, and the gotchas, written to docs/SDK_NOTES.md.
```

| # | Check | Owner | Pass criteria |
|---|---|---|---|
| 0.1 | **MCP over Streamable HTTP from Claude Code.** A minimal FastMCP server (official `mcp` SDK) mounted in FastAPI. Connect with `claude mcp add --transport http mulligan <url> --header "Authorization: Bearer <token>"`. | A | Claude Code lists the tool, calls it, and the server sees the bearer token and the MCP session id. |
| 0.2 | **MCP from Devin.** The same server with MCP on its own port (`:8001`) and a dummy REST route on `127.0.0.1:8000`, both in one process. A free cloudflared quick tunnel (`cloudflared tunnel --url http://localhost:8001`, no account) exposes only the MCP port. Added in Devin under Customize → MCPs → custom → HTTP with an Auth Header. | A | Devin's "Test tools" passes and a Devin session calls the tool. The REST route can't be reached through the tunnel URL, and an MCP call without the bearer token gets a 401. |
| 0.3 | **Postgres typed-tool undo.** Typed write tools (`pg_insert_rows`, `pg_update_rows`, `pg_delete_rows`) build parameterized SQL from a structured filter, never from an SQL string. A generic audit trigger reads `current_setting('mulligan.action_id', true)` (set with `SET LOCAL` per action txn) and writes old/new `jsonb` rows, including rows removed by `ON DELETE CASCADE`. A script runs each tool, then replays the inverse. Undo must put parent rows back before their children: a cascade deletes the parent first, so plain reverse audit order would re-insert children first and break the FK. Either sort re-inserts by FK dependency (parents first) or make FKs `DEFERRABLE` and run undo under `SET CONSTRAINTS ALL DEFERRED`. Pick one here and record it. `pg_drop_table` is a soft drop: `ALTER TABLE ... SET SCHEMA mulligan_trash` plus a rename, and undo moves the table back. Run on TimescaleDB-compatible Postgres. | A | The table checksum after undo equals the checksum before, including a parent delete that cascaded into 2+ child tables. A dropped table comes back with its data, PK, indexes and the foreign keys that point at it, and the agent's read-only role can't see `mulligan_trash`. A runaway `pg_query` is killed by `statement_timeout`. |
| 0.4 | **Notion restore.** Capture a page's properties and full block tree, let a "rogue" edit rewrite and trash it, then restore it. Then repeat, but hand-edit one block the rogue edit wrote before restoring, and spot it with a hash of each property and block the rogue edit wrote. Check which API version is current and its trash field (`archived` vs `in_trash`). | A | The restored page's normalized content hash matches the original. On the repeat, the hand-edited block is kept and flagged, and everything else matches. Note any block types the API can't recreate. |
| 0.5 | **Fetch agent in ASI:One.** A `uagents` agent with `mailbox=True`, `publish_agent_details=True` and the chat protocol (`chat_protocol_spec`, `ChatMessage`, `ChatAcknowledgement`), connected through the Inspector. | A | The agent is findable on Agentverse, and "Chat with Agent" in ASI:One gets a reply. |
| 0.6 | **Photon iMessage send/reply.** `spectrum-ts` with `Spectrum({projectId, projectSecret, providers:[imessage.config()]})`. Send a message, read a reply from `app.messages`. Decide between the cloud and local provider. | B | A phone gets "Reply YES 4821", and the reply is parsed within 5 s. |
| 0.7 | **Stripe manual capture.** A test-mode PaymentIntent with `capture_method=manual`. Confirm with a test card, then capture one and cancel another. Note the authorization expiry window. | B | One intent ends `succeeded`, the other `canceled`. |
| 0.8 | **Sentry MCP monitoring.** Turn on Python SDK tracing and the MCP integration (check its name and minimum versions, since the docs page doesn't say). | B | Sentry shows a `tools/call <name>` span with `mcp.tool.name` and `mcp.session.id`. |

**Exit checklist:** All 8 spikes pass, or have a written fallback (for example 0.2 falls back to using Devin only to build code, not as a protected agent, and 0.6 falls back to the local iMessage provider). `docs/SDK_NOTES.md` is filled in.
**STOP — approval gate.**

---

## Phase 1 — Shared contracts and skeleton (A writes it, B reviews, both approve)

```
GOAL: Freeze the contracts so both streams can build without blocking each other.
Done when: both type sets generate, mock servers run, `docker compose up` brings up both Postgres DBs, and CI is green.
```

### 1.1 `contracts/` (single source of truth)
- `action.schema.json` (JSON Schema) generates `gateway/mulligan/models.py` (Pydantic v2) and `dashboard/src/types.ts` and `approvals-photon/src/types.ts`.

**Action**
```
id                "act_<ulid>"
session_id        "ses_<ulid>"
agent             { id, kind: claude_code|devin|fetch|rogue|other, display_name }
system            postgres|github|notion|email|payments|upstream
tool              MCP tool name, e.g. "pg_delete_rows"
upstream_server   name of the MCP server behind Mulligan, or null for built-in tools
args_redacted     object (secrets stripped)
status            pending|held|approved|rejected|executing|executed|failed|
                  reverting|reverted|conflict|revert_failed|expired
                  (conflict: the rewind kept one or more fields a human changed)
reversibility     { label: read_only|reversible|reversible_noisy|compensable|irreversible,
                    source: policy|rules|override }
                  (policy for every built-in tool; rules only for upstream tools)
before_ref        opaque pointer to the captured before-state (per adapter)
after_ref         opaque pointer to the after-state
after_hashes      { "<field path>": sha256 } hash of every field the action changed,
                                             e.g. "postgres:products/42/price"
inverse           { kind, payload }          adapter-specific undo recipe
approval          { code, channel: imessage|dashboard, requested_at,
                    decided_at, decided_by, decision } | null
hold              { kind: email_outbox|payment_auth, release_at, external_id } | null
explanation       string (plain-English summary, StepFun, nullable)
sentry_trace_id   string
created_at, executed_at, reverted_at
```
**Session** `{ id, agent, mode: live|replay, started_at, ended_at, action_count, status: active|ended|rewound }`
**RewindJob** `{ id, scope: {session_id} | {since: ts} | {action_ids: []}, dry_run, status: planned|running|completed|partial|failed, steps: [{action_id, system, result: restored|kept_human_edit|skipped|failed, kept_fields: [field path], detail}], verification: {<system>: {expected, actual, ok}} }`

Steps run newest action first. `expected` is the checksum of the pre-session state with any kept human edits applied on top, so a kept edit doesn't fail verification.

### 1.2 `contracts/openapi.yaml` (gateway REST, prefix `/api`)
| Method | Path | Purpose | Consumer |
|---|---|---|---|
| GET | `/sessions`, `/sessions/{id}` | list and detail | UI, Fetch |
| GET | `/actions?session_id&since&system&status` and `/actions/{id}` | timeline | UI, Fetch |
| POST | `/rewind` `{scope, dry_run}` returns RewindJob | plan or run a rewind | UI, Fetch, Photon |
| GET | `/rewind/{id}` | job progress | UI, Fetch |
| POST | `/approvals/{action_id}/decision` `{decision, code, channel, decided_by}` | approve or reject a held action | Photon service, UI |
| GET | `/checksums` | `{system: {hash, computed_at}}` | UI, demo |
| GET | `/events` | SSE stream | UI |
| POST | `/demo/reset` | dev only, behind `MULLIGAN_DEMO=1` | demo, UI |

Auth: the UI uses a dashboard token. Service-to-service calls (Photon, Fetch) use `X-Mulligan-Service-Token`. MCP agents use a per-agent bearer token that maps to `agent`. The demo's replay client has its own token, so its sessions are marked `mode: replay`. **Every route needs a token**, enforced by one FastAPI dependency applied to the whole app. The only exception is `GET /healthz`. `/api/demo/reset` needs `MULLIGAN_DEMO=1` *and* the dashboard token.

**Ports (two servers in one gateway process, sharing the action log and event bus):**
| Port | Bind | Serves | Reached by |
|---|---|---|---|
| `8000` (`MULLIGAN_API_PORT`) | `127.0.0.1` only | REST `/api/*`, SSE `/api/events`, `/healthz` | dashboard, Photon service, Fetch agent, demo scripts (all on the laptop) |
| `8001` (`MULLIGAN_MCP_PORT`) | `0.0.0.0` | MCP `/mcp` only (Streamable HTTP) | Claude Code locally, and Devin through the cloudflared quick tunnel |

Under `docker compose`, the bind column means the host port mapping (`"127.0.0.1:8000:8000"` and `"8001:8001"`). Inside the container both servers listen on `0.0.0.0` so the dashboard and Photon containers can reach the gateway over the compose network. The tunnel points at `:8001` only, so the REST API is never public. This is free and needs no Cloudflare account or domain. If the changing quick-tunnel URL becomes a problem, the free upgrade is ngrok's single static domain, still pointed at `:8001` only.

**Photon service API (Stream B serves it, the gateway calls it):** `POST /notify/approval {action_id, code, summary, to}`, `POST /notify/summary {rewind_job_id, text, to}`. Replies get parsed and forwarded to `/api/approvals/{id}/decision`.

### 1.3 `contracts/events.md` (SSE and internal bus, same names)
`session.started`, `session.ended`, `action.created`, `action.classified`, `action.held`, `action.approved`, `action.rejected`, `action.executed`, `action.failed`, `rewind.planned`, `rewind.started`, `rewind.step`, `rewind.completed`, `rewind.failed`, `checksum.verified`, `hold.released`, `hold.expired`. Payload: `{event, ts, data: Action | RewindJob | {...}}`.

### 1.4 `contracts/mcp_tools.md` and `contracts/reversibility_policy.yaml` (what agents see)
Built-in tools: `pg_query`, `pg_insert_rows`, `pg_update_rows`, `pg_delete_rows`, `pg_drop_table`, `github_list_issues`, `github_close_issue`, `github_comment`, `github_update_file`, `notion_get_page`, `notion_update_page`, `notion_trash_page`, `email_send` (held), `payments_charge` (held), `mulligan_status`. Each entry lists its input schema and its adapter.
- **No raw SQL writes.** Postgres write tools take a table, a structured filter (`[{column, op, value}]`) and values, so Mulligan knows which rows a call touches before it runs. Deleting every row needs an explicit `all: true`. `pg_query` accepts SQL but runs under a read-only role in a `READ ONLY` transaction, so it can't change anything. It also gets a `statement_timeout` (default 5 s, `PG_QUERY_TIMEOUT_MS`) and a row cap, so a read can't hang the gateway or the DB.
- **Soft drop.** `pg_drop_table` moves the table into the `mulligan_trash` schema, which agents can't see, and renames it `<table>__<action_id>`. Undo moves it back. Trashed tables are purged only after the undo window closes.
  - **Known difference from a real `DROP`:** moving a table keeps every foreign key that points at it, and so does a rename. A real `DROP` without `CASCADE` would fail on those FKs, and `DROP ... CASCADE` would remove them. Mulligan does neither: the drop always succeeds, referencing tables keep working against the trashed table, and undo needs no FK repair. The tool's result tells the agent which FKs still reference the table, and the action log records them. This is deliberate. Don't "fix" it by dropping FKs.
- **Undo order inside one action.** An action's audit rows are reverted newest first, except that re-inserted rows go parents first (the method chosen in spike 0.3), so cascaded deletes come back without FK errors.
- **Policy table.** `reversibility_policy.yaml` gives every built-in tool its label. It is the authority: the gateway won't start if a built-in tool has no entry, and the rules gate is never asked about built-in tools.
- **Upstream tools.** Tools from MCP servers placed behind Mulligan are exposed as `upstream__<server>__<tool>`. They have no policy entry and no undo adapter, so every call goes through the rules gate (1.5).

### 1.5 Python interfaces (`gateway/mulligan/interfaces.py`)
- `Adapter.capture_before(call) -> ref`, `execute(call) -> result`, `capture_after(...) -> (ref, after_hashes)`, `build_inverse(...)`, `revert(action) -> StepResult`, `checksum() -> str`. `revert` re-reads every field in `after_hashes` and restores only the fields whose current hash still matches. The others come back as `kept_human_edit` in `kept_fields`. Nothing is merged.
- `HoldManager.hold(action)`, `on_decision(action_id, decision)`, `release_due()`, `cancel(action)`
- `Policy.lookup(tool) -> Reversibility | None` reads `contracts/reversibility_policy.yaml`.
- `Classifier.classify_unknown(tool_schema) -> Reversibility` runs only when `Policy.lookup` returns None. It is a deterministic rules check, not a model. The gateway lets the call through only if the label is `read_only`. Any other label, or an error, holds the call for approval (fail closed). Stream A ships a stub that always returns `irreversible` (hold everything), and Stream B replaces it with the rules gate. The interface stays, so a trained model could be swapped in later without touching the gateway.

### 1.6 Skeleton
The folder layout already exists (see "Repo structure" above). Phase 1 adds `pyproject.toml` / `package.json` per package, the `Makefile`, and `docker-compose.yml`: `mulligan-db` (timescale/timescaledb), `prod-db` (postgres:16, the demo company, with the audit triggers, a read-only role for `pg_query` and the `mulligan_trash` schema), `gateway`, `approvals-photon`, `dashboard`. Mock servers come from the OpenAPI spec (Prism) so Stream B's UI can start right away. CI runs lint, type checks, unit tests and contract type generation with a diff check.

**Exit checklist:** Both people sign off on `contracts/`. Mocks run. Compose is up. CI is green.
**STOP — approval gate.**

---

## Phase 2 — First vertical slice (parallel)

**Stream A**
```
GOAL: An agent connected over MCP changes prod-db through the typed Postgres tools, every action is logged with before/after and field hashes, and POST /api/rewind for that session restores the DB with a matching checksum.
```
- Gateway: two uvicorn servers in one process: FastMCP (Streamable HTTP) on `:8001`, REST and SSE on `127.0.0.1:8000`. Token auth on every route, per-agent bearer auth on MCP, session tracking, action log in TimescaleDB (`actions` hypertable plus `events`), SSE `/api/events`.
- Postgres adapter, building on the 0.3 spike: the typed tools build parameterized SQL, one txn per action with `SET LOCAL mulligan.action_id`, audit-trigger capture, a hash for each (row, column) the action changed, the soft drop into `mulligan_trash`, and the inverse built from the audit rows (parents re-inserted before children). `pg_query` runs read-only with `statement_timeout` and a row cap.
- Undo orchestrator v1: one system, newest action first, dry-run plan, then execute and verify the checksum.
- Policy table: an entry in `contracts/reversibility_policy.yaml` for every registered tool, checked at startup. Ship the hold-everything `Classifier` stub.
- Sentry: a span per action and per rewind step, with `sentry_trace_id` stored on the action.
- Tests: an integration suite against compose (pytest), plus Claude Code connecting live.

**Stream B**
```
GOAL: The Photon service delivers an approval text for a held action and turns the reply into an approve/reject decision on the gateway (mock or real). The dashboard shell renders sessions and actions from the mock API.
```
- `approvals-photon/` (TS): spectrum-ts client, 4-digit codes, reply parsing (`YES 4821` / `NO 4821`), retry handling, the `/notify/*` endpoints.
- `gateway/mulligan/holds/`: approval state machine (`held` to `approved`/`rejected`/`expired`) behind the `HoldManager` interface.
- Dashboard: React and Vite scaffold, typed API client, Sentry React with Session Replay, session list and action timeline driven by the mock and SSE. Start the Paper design in parallel.

**Integration checkpoint (end of phase):** Point the UI and Photon service at the real gateway. A `pg_delete_rows` call from Claude Code shows up live in the UI.
**STOP — approval gate.**

---

## Phase 3 — Multi-system undo, holds and rules gate (parallel)

**Stream A**
```
GOAL: One rewind restores Postgres, GitHub and Notion together, newest action first. Any field a human changed after the agent is kept and flagged, and each system's checksum matches the expected state.
```
- GitHub adapter (test org): close/reopen issue, comment create/delete, file update reverted through the contents API with sha checks. Conflict detection: hash each issue field the agent changed (state, title, body, labels, assignees) and each file's blob sha. At rewind time, a field or file whose current hash differs is kept and flagged. No three-way merge.
- Notion adapter, building on the 0.4 spike: block-tree snapshot and restore, untrash, property restore. Conflict detection: hash each property and block the agent wrote. A property or block whose hash changed is kept and flagged, and the rest of the page is restored.
- Postgres conflict detection: compare each (row, column) hash in `after_hashes` with the current value. A changed column is kept and flagged, and the row's other columns are still restored. If a human reused the primary key of a row the agent deleted, or the name of a table it dropped, that step is a conflict and is skipped.
- Orchestrator v2: a cross-system plan in reverse time order (no dependency graph), `rewind to timestamp`, partial-failure reporting, and per-system verification against the expected state.
- Upstream MCP pass-through: register MCP servers behind Mulligan (`UPSTREAM_MCP_SERVERS`), expose their tools as `upstream__<server>__<tool>`, and send every call through `Policy.lookup` and the rules gate. These calls are logged but can't be undone. `demo/upstream/` holds a small mock Slack MCP server (`list_channels`, `post_message`) for the demo.

**Stream B**
```
GOAL: email_send and payments_charge are held. The iMessage approval releases them, rewind cancels payments that haven't been captured, every built-in tool gets its label from the policy table, and upstream tools go through the rules gate.
```
- Email hold: an outbox table. Send only after approval, through a sandbox provider or Mailpit in the demo. After a send, the action is `irreversible`, and rewind marks it `skipped` and offers a compensating correction email.
- Payments: Stripe test mode with `capture_method=manual`. Capture when `release_at` passes the undo window, and cancel on rewind.
- Rules gate: replace Stream A's hold-everything stub with a deterministic check over the tool schema, same interface. A tool is `read_only` only if its name starts with a read verb (`get`, `list`, `search`, `read`, `fetch`, `describe`), its description has no write verb (`send`, `post`, `create`, `update`, `delete`, `remove`, `pay`, `charge`, `invite`, `publish`), and the MCP `readOnlyHint` annotation isn't false. Everything else is held. Unit tests cover both mock Slack tools, plus our built-in tools as a sanity check: only the read tools (`pg_query`, `github_list_issues`, `notion_get_page`, `mulligan_status`) may come out `read_only`. The label's reason (which rule fired) is stored so the dashboard can explain why a call was held.
- Dashboard: timeline with system lanes, a held-actions panel, the rewind dialog (dry-run diff, then run), live rewind progress, a checksum panel, Sentry trace links, a "not undoable" marker on upstream calls and a "replay" badge on replay sessions.

**Integration checkpoint:** A scripted 6-action session across all five systems, plus one upstream `post_message` call that gets held, rewinds cleanly, and the UI shows every step.
**STOP — approval gate.**

---

## Phase 4 — Demo, agents and dashboard polish (parallel)

**Stream A**
```
GOAL: `make demo-reset && make demo-replay` reproduces the pitch scenario deterministically every time, `make demo-live` runs it with a live LLM agent, and the Guardian agent runs it from ASI:One chat.
```
- `demo/seed`: a fake company ("Acme Widgets") with customers, orders, products with prices, a legacy pricing table, 40+ GitHub issues and a Notion pricing doc. Use fixed seeds and Faker so data is identical across runs.
- `demo/rogue_agent.py` has two modes. `--live` runs a real LLM agent connected over MCP with only Mulligan's tools and an over-broad cleanup task, worded so that it deletes inactive customers, updates product prices, drops the legacy pricing table, closes 40 stale issues, rewrites the pricing doc, emails every customer (held) and makes a charge (held). Every tool call is recorded to `demo/recordings/<timestamp>.json`. `--replay <file>` is a scripted MCP client that sends a recording's calls through Mulligan with deterministic timing. Commit one known-good recording as the default. Replay is the fallback when the venue network or the model misbehaves, and its sessions show as `mode: replay`.
- `demo/teammate_edit.py`: human edits written straight to Postgres and Notion (not through Mulligan) after the agent runs. Two of them change something the agent also changed (a product price, and a price in the pricing doc), so the rewind keeps them and flags them. A third touches something the agent never changed.
- `demo/reset`: one command that wipes and reseeds prod-db, GitHub and Notion, clears the log and prints checksums. `demo/checksums.py` computes per-system hashes from canonical, sorted, normalized output.
- `fetch-agent/`: a Guardian agent with the chat protocol and a worker agent (multi-agent). It runs a protected task, reports what's held, and rewinds when the user says "rewind". Payment Protocol fee is a stretch goal.
- Connect Devin to Mulligan as a protected agent, and use Devin to build one piece (suggestion: the seed generator or the GitHub adapter tests).

**Stream B**
```
GOAL: The dashboard is demo-ready to the Paper design, and every held action shows why it was held.
```
- StepFun `step-5-preview` explanations per action (optional, cached, async so the action path never waits on it).
- Dashboard polish: the "Rewind" button moment, before/after diffs per system, kept-human-edit flags (which field, the human's value and what the agent had written), a checksum proof view (expected vs. actual) and the iMessage summary after a rewind.

**Integration checkpoint:** A full rehearsal of the pitch: the rogue agent runs, the phone buzzes, Mulligan restores everything, the teammate's edits are kept and flagged, and checksums match. Run the replay 3 times in a row and the live run at least once.
**STOP — approval gate.**

---

## Phase 5 — Hardening, observability and demo-laptop setup (parallel)

```
GOAL: `docker compose up` on the demo laptop runs the whole stack (both DBs, gateway, Photon service, dashboard) with no paid hosting. Only the MCP port is reachable through the tunnel, and every route rejects a missing or wrong token.
```

**Stream A:** Run the gateway and both DBs on the demo laptop under `docker compose`. A free cloudflared quick tunnel points at the MCP port (`:8001`) only, giving Devin a public HTTPS URL. A quick tunnel's URL changes whenever it restarts, so keep it running and update Devin's MCP settings if it does. Add a check script that hits the tunnel URL and confirms `/api/*` isn't reachable there, and that every local route returns 401 without a token. Set up Sentry Logs and AI agent monitoring, backups of the action log, and a load check (200 actions in the log, rewind under 30 s). Add a Claude Code `PreToolUse` hook config (docs check against code.claude.com/docs/en/hooks) for demo laptops that blocks direct DB, `gh` and curl calls, so the only route is Mulligan.
**Stream B:** Add the dashboard (built with Vite and served from its compose service) and the Photon service to `docker compose`, both talking to the gateway on `127.0.0.1:8000`. Make sure Session Replay, error boundaries, empty and error states, and phone-width layout all work. Prepare the sponsor-evidence page (how Sentry data shaped the build, and the Photon flow).

**STOP — approval gate.**

---

## Phase 6 — Final review (2 iterations, both streams)

```
GOAL: No known correctness bug in the undo path, no secret reachable by an agent, and the demo passes 5 clean replay runs in a row plus 2 live runs on the demo laptop.
```
- **Review iteration 1:** Each person runs `/code-review high` on the other stream's code, then `/security-review` (credential isolation, token scoping, `pg_query`'s read-only enforcement and the typed tools' SQL building, SSRF in adapters and the upstream pass-through). Check the contracts against the implementation, fill test-coverage gaps on revert and conflict-detection paths, and fix everything found.
- **Review iteration 2:** Re-run the reviews on the fix diff and run the full end-to-end demo on the demo laptop 5 times from the replay and twice live. Fix remaining bugs, freeze `main` and tag `v1-demo`.

**STOP — final approval.**

---

## Open questions to settle before Phase 1
1. Who is Stream A and who is Stream B.
2. Visa: which Visa tools mentors expect, if Stripe test mode alone isn't enough.
3. Email sandbox: Mailpit (local only) or a real sandbox provider such as Resend test mode.
4. The iMessage line: Photon cloud line or local provider on a team Mac.
5. Which model and API key drive the live rogue agent.
