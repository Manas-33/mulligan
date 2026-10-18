# Mulligan

**A do-over for AI agents.** Agents connect to Mulligan as an MCP server, and Mulligan holds the real credentials. Agents get typed tools, never raw SQL. Mulligan records every action with its before-state, undoes an agent's session across Postgres, GitHub and Notion while keeping any edits people made since, and holds irreversible actions (email, payments) until someone approves them over iMessage.

- Idea: [MULLIGAN_IDEA.md](MULLIGAN_IDEA.md)
- Plan (phases, contracts, workstreams): [PLAN.md](PLAN.md)
- How we work together: [CONTRIBUTING.md](CONTRIBUTING.md)
- SDK versions and gotchas: [docs/SDK_NOTES.md](docs/SDK_NOTES.md)

## Repo layout

```
mulligan/
├── contracts/            # SHARED. Action schema, OpenAPI, events, MCP tools, reversibility policy. Changes need both people.
│   └── generated/        #   generated types (do not hand-edit)
├── gateway/              # Python 3.12, FastAPI + official `mcp` SDK (Streamable HTTP)
│   ├── mulligan/
│   │   ├── api/          #   A: REST /api/* + SSE /api/events
│   │   ├── mcp_server/   #   A: MCP server, typed tool registry, upstream pass-through
│   │   ├── log/          #   A: action log (TimescaleDB)
│   │   ├── orchestrator/ #   A: rewind planning (newest action first), verification
│   │   ├── adapters/     #   A: postgres/ github/ notion/  (real undo, conflict detection)
│   │   ├── holds/        #   B: email + payments held channels, approval state machine
│   │   └── classify/     #   B: policy lookup + rules gate for unknown upstream tools
│   ├── migrations/       #   A: mulligan-db schema
│   └── tests/
├── approvals-photon/     # B: TypeScript, spectrum-ts iMessage approvals service
├── dashboard/            # B: React + Vite. All UI lives here and only here.
├── fetch-agent/          # A: uAgents Guardian + worker agents (ASI:One)
├── demo/                 # A: seeded fake company, rogue agent (live + replay), mock upstream MCP server, reset, checksums
├── infra/                # A: docker-compose DB init, cloudflared tunnel for Devin
├── spikes/               # Phase 0 throwaway de-risk scripts
├── docs/
└── .github/workflows/    # CI
```

**A** = Stream A (Core and Undo). **B** = Stream B (Holds, Approvals, Rules Gate, all UI). See the ownership rules in [CONTRIBUTING.md](CONTRIBUTING.md).

## Getting started

Needs Docker, [uv](https://docs.astral.sh/uv/), Node 22 (for the mocks) and, for Devin, cloudflared.

```bash
cp .env.example .env   # sandbox/test credentials only; set the three MULLIGAN_*_TOKEN(S) values
make setup             # gateway Python deps
make db                # mulligan-db (TimescaleDB) and prod-db (the demo company), on 127.0.0.1:5433/5434
make gateway           # REST + SSE on 127.0.0.1:8000, MCP on 127.0.0.1:8001/mcp
```

`make up` runs the whole stack under docker compose instead. `make help` lists every target.

### Connect an agent

Each agent gets its own token in `MULLIGAN_AGENT_TOKENS` (`claude_code:<token>,devin:<token>,rogue:<token>,rogue_replay:<token>`).

```bash
claude mcp add --transport http mulligan http://127.0.0.1:8001/mcp --header "Authorization: Bearer <claude_code token>"
```

On a demo laptop, run Claude Code from `infra/demo-agent/`: it is wired to Mulligan, and a hook blocks direct database, GitHub and Notion access so Mulligan is the only route.

For Devin, `make tunnel` gives the MCP port (only) a public `https://….trycloudflare.com` URL. Add it in Devin under Customize → MCPs → custom → HTTP, with the URL ending in `/mcp` and an `Authorization: Bearer <devin token>` header.

### The demo

```bash
make demo-reset        # reseed every system, clear the log, print checksums
make demo-replay       # the rogue agent's recorded run (or: make demo-live)
make teammate-edit     # a teammate fixes a price by hand after the agent
```

Then rewind the session from the dashboard, or with `POST /api/rewind {"scope": {"session_id": …}, "dry_run": false}`.

### Checks

```bash
make ci                # contract types up to date, OpenAPI lint, ruff, mypy, unit tests
make test-integration  # against `make db`
make check-exposure    # every route needs a token; only /mcp is reachable through the tunnel
make load-check        # 200 actions rewound in under 30 s
make mocks             # Prism mocks of the gateway (:4010) and Photon (:4011) APIs
```
