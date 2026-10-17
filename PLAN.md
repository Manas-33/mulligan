# Rewind — Implementation Plan

Source: `REWIND_IDEA.md`. No code until this plan is approved.

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

| | **Stream A — Core and Undo** | **Stream B — Holds, Approvals, Classifier and all UI** |
|---|---|---|
| Owns | `gateway/` core (MCP server, action log, undo orchestrator), `gateway/rewind/adapters/{postgres,github,notion}`, `demo/` (seed company, rogue agent, reset, checksums), `fetch-agent/`, `infra/` | `dashboard/` (**all UI, sole owner**), `approvals-photon/` (TS), `gateway/rewind/holds/` (email and payments adapters, approval state machine), `gateway/rewind/classify/` and `classifier/` (dataset and Veri training) |
| Sponsor surface | Fetch.ai, Devin, MLH (Tiger Data, Vultr, GoDaddy), Sentry backend | Photon, Visa/Stripe, veri.studio, Paper, Sentry frontend (Session Replay), StepFun |

Stream B owns the UI outright, so Stream A ships no UI code. Stream A's only UI-facing outputs are the REST API and the SSE events in the contracts.

**Collision rules.** Stream B's code inside `gateway/` lives only in `holds/` and `classify/`. It plugs into core through the `Adapter`, `HoldManager` and `Classifier` interfaces in `contracts/`. Shared files (`gateway/rewind/app.py`, `pyproject.toml`, `docker-compose.yml`) belong to Stream A. Stream B asks for changes to them in a PR.

## Repo structure (already in place on `main`)

Every folder has a README that states its owner and scope. Ownership and git rules are in [CONTRIBUTING.md](CONTRIBUTING.md).

```
rewind/
├── README.md  PLAN.md  CONTRIBUTING.md  REWIND_IDEA.md
├── .env.example  .gitignore  .editorconfig
├── contracts/                 # SHARED. Changes need both people's approval
│   └── generated/             #   generated Pydantic + TS types (don't hand-edit)
├── gateway/                   # Python 3.12, FastAPI + official `mcp` SDK
│   ├── rewind/
│   │   ├── mcp/               #   A: MCP server (Streamable HTTP), tool registry, agent auth
│   │   ├── api/               #   A: REST /api/* + SSE /api/events
│   │   ├── log/               #   A: action log (TimescaleDB)
│   │   ├── orchestrator/      #   A: rewind plan, reverse dependency order, verify
│   │   ├── adapters/
│   │   │   ├── postgres/      #   A
│   │   │   ├── github/        #   A
│   │   │   └── notion/        #   A
│   │   ├── holds/             #   B: email + payments holds, approval state machine
│   │   └── classify/          #   B: classifier client + rules fallback
│   ├── migrations/            #   A: rewind-db schema
│   └── tests/
├── approvals-photon/          # B: TypeScript spectrum-ts service (src/, test/)
├── dashboard/                 # B: React + Vite. ALL UI (src/, public/)
├── classifier/                # B: data/ (APIs.guru → SFT JSONL), configs/ (Veri), eval/
├── fetch-agent/               # A: uAgents Guardian + worker (tests/)
├── demo/                      # A: seed/, scripts/ (rogue agent, teammate edit, reset), checksums/
├── infra/                     # A: db/rewind-db/, db/prod-db/ (audit triggers), deploy/
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
| 0.1 | **MCP over Streamable HTTP from Claude Code.** A minimal FastMCP server (official `mcp` SDK) mounted in FastAPI. Connect with `claude mcp add --transport http rewind <url> --header "Authorization: Bearer <token>"`. | A | Claude Code lists the tool, calls it, and the server sees the bearer token and the MCP session id. |
| 0.2 | **MCP from Devin.** The same server exposed publicly (cloudflared tunnel or a Vultr box), added in Devin under Customize → MCPs → custom → HTTP with an Auth Header. | A | Devin's "Test tools" passes and a Devin session calls the tool. |
| 0.3 | **Postgres trigger undo.** A generic audit trigger reads `current_setting('rewind.action_id', true)` (set with `SET LOCAL` per action txn) and writes old/new `jsonb` rows. A script runs UPDATE/DELETE/INSERT, then replays the inverse. Also: snapshot a table before `DROP`/`TRUNCATE` (detected with `sqlglot`) and restore it. Run on TimescaleDB-compatible Postgres. | A | The table checksum after undo equals the checksum before. A DROPped table comes back with its data, PK and indexes. |
| 0.4 | **Notion restore.** Capture a page's properties and full block tree, let a "rogue" edit rewrite and trash it, then restore it. Check which API version is current and its trash field (`archived` vs `in_trash`). | A | The restored page's normalized content hash matches the original. Note any block types the API can't recreate. |
| 0.5 | **Fetch agent in ASI:One.** A `uagents` agent with `mailbox=True`, `publish_agent_details=True` and the chat protocol (`chat_protocol_spec`, `ChatMessage`, `ChatAcknowledgement`), connected through the Inspector. | A | The agent is findable on Agentverse, and "Chat with Agent" in ASI:One gets a reply. |
| 0.6 | **Photon iMessage send/reply.** `spectrum-ts` with `Spectrum({projectId, projectSecret, providers:[imessage.config()]})`. Send a message, read a reply from `app.messages`. Decide between the cloud and local provider. | B | A phone gets "Reply YES 4821", and the reply is parsed within 5 s. |
| 0.7 | **Stripe manual capture.** A test-mode PaymentIntent with `capture_method=manual`. Confirm with a test card, then capture one and cancel another. Note the authorization expiry window. | B | One intent ends `succeeded`, the other `canceled`. |
| 0.8 | **Sentry MCP monitoring.** Turn on Python SDK tracing and the MCP integration (check its name and minimum versions, since the docs page doesn't say). | B | Sentry shows a `tools/call <name>` span with `mcp.tool.name` and `mcp.session.id`. |
| 0.9 | **Veri credits and CLI.** Redeem credits, then run `veri login`, `veri init`, `veri datasets check` on a 10-row JSONL and `veri jobs estimate`. | B | The estimate returns and the balance is above 0. |

**Exit checklist:** All 9 spikes pass, or have a written fallback (for example 0.2 falls back to Devin over a public Vultr URL, and 0.6 falls back to the local iMessage provider). `docs/SDK_NOTES.md` is filled in.
**STOP — approval gate.**

---

## Phase 1 — Shared contracts and skeleton (A writes it, B reviews, both approve)

```
GOAL: Freeze the contracts so both streams can build without blocking each other.
Done when: both type sets generate, mock servers run, `docker compose up` brings up both Postgres DBs, and CI is green.
```

### 1.1 `contracts/` (single source of truth)
- `action.schema.json` (JSON Schema) generates `gateway/rewind/models.py` (Pydantic v2) and `dashboard/src/types.ts` and `approvals-photon/src/types.ts`.

**Action**
```
id                "act_<ulid>"
session_id        "ses_<ulid>"
agent             { id, kind: claude_code|devin|fetch|rogue|other, display_name }
system            postgres|github|notion|email|payments
tool              MCP tool name, e.g. "pg_execute"
args_redacted     object (secrets stripped)
status            pending|held|approved|rejected|executing|executed|failed|
                  reverting|reverted|merged|conflict|revert_failed|expired
reversibility     { label: reversible|reversible_noisy|compensable|irreversible,
                    confidence, source: model|rules|override }
before_ref        opaque pointer to the captured before-state (per adapter)
after_ref         opaque pointer to the after-state
inverse           { kind, payload }          adapter-specific undo recipe
depends_on        [action_id]                drives reverse dependency order
approval          { code, channel: imessage|dashboard, requested_at,
                    decided_at, decided_by, decision } | null
hold              { kind: email_outbox|payment_auth, release_at, external_id } | null
explanation       string (plain-English summary, StepFun, nullable)
sentry_trace_id   string
created_at, executed_at, reverted_at
```
**Session** `{ id, agent, started_at, ended_at, action_count, status: active|ended|rewound }`
**RewindJob** `{ id, scope: {session_id} | {since: ts} | {action_ids: []}, dry_run, status: planned|running|completed|partial|failed, steps: [{action_id, system, result: restored|merged|kept_human_edit|skipped|failed, detail}], verification: {<system>: {expected, actual, ok}} }`

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
| POST | `/demo/reset` | dev only, behind `REWIND_DEMO=1` | demo, UI |

Auth: the UI uses a dashboard token. Service-to-service calls (Photon, Fetch) use `X-Rewind-Service-Token`. MCP agents use a per-agent bearer token that maps to `agent`.

**Photon service API (Stream B serves it, the gateway calls it):** `POST /notify/approval {action_id, code, summary, to}`, `POST /notify/summary {rewind_job_id, text, to}`. Replies get parsed and forwarded to `/api/approvals/{id}/decision`.

### 1.3 `contracts/events.md` (SSE and internal bus, same names)
`session.started`, `session.ended`, `action.created`, `action.classified`, `action.held`, `action.approved`, `action.rejected`, `action.executed`, `action.failed`, `rewind.planned`, `rewind.started`, `rewind.step`, `rewind.completed`, `rewind.failed`, `checksum.verified`, `hold.released`, `hold.expired`. Payload: `{event, ts, data: Action | RewindJob | {...}}`.

### 1.4 `contracts/mcp_tools.md` (what agents see)
`pg_query`, `pg_execute`, `github_list_issues`, `github_close_issue`, `github_comment`, `github_update_file`, `notion_get_page`, `notion_update_page`, `notion_trash_page`, `email_send` (held), `payments_charge` (held), `rewind_status`. Each entry lists its input schema, its adapter and its default reversibility.

### 1.5 Python interfaces (`gateway/rewind/interfaces.py`)
- `Adapter.capture_before(call) -> ref`, `execute(call) -> result`, `capture_after(...)`, `build_inverse(...)`, `revert(action, policy) -> StepResult`, `checksum() -> str`
- `HoldManager.hold(action)`, `on_decision(action_id, decision)`, `release_due()`, `cancel(action)`
- `Classifier.classify(tool, args) -> Reversibility`. Stream A ships a rules stub, Stream B replaces it.

### 1.6 Skeleton
The folder layout already exists (see "Repo structure" above). Phase 1 adds `pyproject.toml` / `package.json` per package, the `Makefile`, and `docker-compose.yml`: `rewind-db` (timescale/timescaledb), `prod-db` (postgres:16, the demo company), `gateway`, `approvals-photon`, `dashboard`. Mock servers come from the OpenAPI spec (Prism) so Stream B's UI can start right away. CI runs lint, type checks, unit tests and contract type generation with a diff check.

**Exit checklist:** Both people sign off on `contracts/`. Mocks run. Compose is up. CI is green.
**STOP — approval gate.**

---

## Phase 2 — First vertical slice (parallel)

**Stream A**
```
GOAL: An agent connected over MCP runs SQL against prod-db, every action is logged with before/after, and POST /api/rewind for that session restores the DB with a matching checksum.
```
- Gateway: FastAPI with FastMCP mounted (Streamable HTTP), per-agent bearer auth, session tracking, action log in TimescaleDB (`actions` hypertable plus `events`), SSE `/api/events`.
- Postgres adapter, building on the 0.3 spike: one txn per action with `SET LOCAL rewind.action_id`; a sqlglot pre-parse that snapshots before DROP/TRUNCATE; inverse built from the audit rows.
- Undo orchestrator v1: one system, reverse chronological order, dry-run plan, then execute and verify the checksum.
- Sentry: a span per action and per rewind step, with `sentry_trace_id` stored on the action.
- Tests: an integration suite against compose (pytest), plus Claude Code connecting live.

**Stream B**
```
GOAL: The Photon service delivers an approval text for a held action and turns the reply into an approve/reject decision on the gateway (mock or real). The dashboard shell renders sessions and actions from the mock API.
```
- `approvals-photon/` (TS): spectrum-ts client, 4-digit codes, reply parsing (`YES 4821` / `NO 4821`), retry handling, the `/notify/*` endpoints.
- `gateway/rewind/holds/`: approval state machine (`held` to `approved`/`rejected`/`expired`) behind the `HoldManager` interface.
- Dashboard: React and Vite scaffold, typed API client, Sentry React with Session Replay, session list and action timeline driven by the mock and SSE. Start the Paper design in parallel.

**Integration checkpoint (end of phase):** Point the UI and Photon service at the real gateway. A `pg_execute` run by Claude Code shows up live in the UI.
**STOP — approval gate.**

---

## Phase 3 — Multi-system undo, holds and classifier (parallel)

**Stream A**
```
GOAL: One rewind restores Postgres, GitHub and Notion together in reverse dependency order, keeps a human edit made after the agent, and verifies each system's checksum.
```
- GitHub adapter (test org): close/reopen issue, comment create/delete, file update reverted through the contents API with sha checks. Human-edit detection uses `updated_at` and ETag, with a three-way merge for files.
- Notion adapter, building on the 0.4 spike: block-tree snapshot and restore, untrash, property restore. Human-edit detection uses `last_edited_by` and `last_edited_time` per block, and keeps blocks a human edited.
- Postgres merge policy: an audit row with a null `action_id` after the agent's change means a human edited it. Revert only the columns the agent changed that the human didn't touch. Otherwise mark `kept_human_edit`.
- Orchestrator v2: a `depends_on` graph, a cross-system plan, `rewind to timestamp`, partial-failure reporting, and per-system verification.

**Stream B**
```
GOAL: email_send and payments_charge are held. The iMessage approval releases them, rewind cancels payments that haven't been captured, and every action carries a reversibility label from the classifier service.
```
- Email hold: an outbox table. Send only after approval, through a sandbox provider or Mailpit in the demo. After a send, the action is `irreversible`, and rewind marks it `skipped` and offers a compensating correction email.
- Payments: Stripe test mode with `capture_method=manual`. Capture when `release_at` passes the undo window, and cancel on rewind.
- Classifier v0: a rules engine with the same interface. Dataset builder: APIs.guru operations plus our MCP tools, auto-labeled by HTTP method and path heuristics, then hand-corrected, written as SFT JSONL (`prompt`, `answer`).
- Dashboard: timeline with system lanes, a held-actions panel, the rewind dialog (dry-run diff, then run), live rewind progress, a checksum panel and Sentry trace links.

**Integration checkpoint:** A scripted 6-action session across all five systems rewinds cleanly, and the UI shows every step.
**STOP — approval gate.**

---

## Phase 4 — Demo, agents and the trained model (parallel)

**Stream A**
```
GOAL: `make demo-reset && make demo-run` reproduces the pitch scenario deterministically every time, and the Guardian agent runs it from ASI:One chat.
```
- `demo/seed`: a fake company ("Acme Widgets") with customers, orders, a pricing table, 40+ GitHub issues and a Notion pricing doc. Use fixed seeds and Faker so data is identical across runs.
- `demo/rogue_agent.py`: a scripted MCP client that deletes customers, drops a table, closes 40 issues, rewrites the pricing doc, sends an email to all customers (held) and makes a charge (held). Timing is deterministic.
- `demo/teammate_edit.py`: a human edit made after the agent, in Postgres and Notion, that must survive the rewind.
- `demo/reset`: one command that wipes and reseeds prod-db, GitHub and Notion, clears the log and prints checksums. `demo/checksums.py` computes per-system hashes from canonical, sorted, normalized output.
- `fetch-agent/`: a Guardian agent with the chat protocol and a worker agent (multi-agent). It runs a protected task, reports what's held, and rewinds when the user says "rewind". Payment Protocol fee is a stretch goal.
- Connect Devin to Rewind as a protected agent, and use Devin to build one piece (suggestion: the seed generator or the GitHub adapter tests).

**Stream B**
```
GOAL: A Veri-trained classifier is served and used by the gateway with >=90% accuracy on a held-out set, and the dashboard is demo-ready to the Paper design.
```
- Fine-tune Qwen3-0.6B or 1.7B on Veri (SFT, push merged weights to HF if using LoRA, deploy with `veri deployments create`). Use a gateway client with a timeout that falls back to rules. Publish an eval report.
- StepFun `step-5-preview` explanations per action (optional, cached, async so the action path never waits on it).
- Dashboard polish: the "Rewind" button moment, before/after diffs per system, a kept-human-edit badge, a checksum proof view and the iMessage summary after a rewind.

**Integration checkpoint:** A full rehearsal of the pitch: the rogue agent runs, the phone buzzes, Rewind restores everything, the teammate's edit survives and checksums match. Run it 3 times in a row.
**STOP — approval gate.**

---

## Phase 5 — Hardening, observability and deploy (parallel)

**Stream A:** Deploy to Vultr (gateway plus both DBs) behind the GoDaddy domain with TLS. Set up a Sentry Uptime monitor on `/healthz`, Sentry Logs and AI agent monitoring, backups of the action log, and a load check (200 actions in the log, rewind under 30 s). Add a Claude Code `PreToolUse` hook config (docs check against code.claude.com/docs/en/hooks) for demo laptops that blocks direct DB, `gh` and curl calls, so the only route is Rewind.
**Stream B:** Deploy the dashboard and Photon service. Make sure Session Replay, error boundaries, empty and error states, and phone-width layout all work. Prepare the sponsor-evidence page (how Sentry data shaped the build, the Veri eval and the Photon flow).

**STOP — approval gate.**

---

## Phase 6 — Final review (2 iterations, both streams)

```
GOAL: No known correctness bug in the undo path, no secret reachable by an agent, and the demo passes 5 clean runs in a row on the deployed stack.
```
- **Review iteration 1:** Each person runs `/code-review high` on the other stream's code, then `/security-review` (credential isolation, token scoping, SQL handling, SSRF in adapters). Check the contracts against the implementation, fill test-coverage gaps on revert and merge paths, and fix everything found.
- **Review iteration 2:** Re-run the reviews on the fix diff and do a full end-to-end demo on the deployed stack 5 times. Fix remaining bugs, freeze `main` and tag `v1-demo`.

**STOP — final approval.**

---

## Open questions to settle before Phase 1
1. Who is Stream A and who is Stream B.
2. Visa: which Visa tools mentors expect, if Stripe test mode alone isn't enough.
3. Email sandbox: Mailpit (local only) or a real sandbox provider such as Resend test mode.
4. The iMessage line: Photon cloud line or local provider on a team Mac.
