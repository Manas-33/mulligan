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
├── approvals-photon/     # B: TypeScript, Photon 2.0 (@photon-ai/api) iMessage approvals service
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

```bash
cp .env.example .env   # fill in sandbox/test credentials only
```

Run instructions get added in Phase 1, along with `docker-compose.yml` and the `Makefile`.
