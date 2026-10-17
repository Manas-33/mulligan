# Rewind

**Ctrl+Z for AI agents.** Agents connect to Rewind as an MCP server, and Rewind holds the real credentials. It records every action with its before-state, undoes an agent's session across Postgres, GitHub and Notion, and holds irreversible actions (email, payments) until someone approves them over iMessage.

- Idea: [REWIND_IDEA.md](REWIND_IDEA.md)
- Plan (phases, contracts, workstreams): [PLAN.md](PLAN.md)
- How we work together: [CONTRIBUTING.md](CONTRIBUTING.md)
- SDK versions and gotchas: [docs/SDK_NOTES.md](docs/SDK_NOTES.md)

## Repo layout

```
rewind/
├── contracts/            # SHARED. Action schema, OpenAPI, events, MCP tools. Changes need both people.
│   └── generated/        #   generated types (do not hand-edit)
├── gateway/              # Python 3.12, FastAPI + official `mcp` SDK (Streamable HTTP)
│   ├── rewind/
│   │   ├── api/          #   A: REST /api/* + SSE /api/events
│   │   ├── mcp/          #   A: MCP server + tool registry
│   │   ├── log/          #   A: action log (TimescaleDB)
│   │   ├── orchestrator/ #   A: rewind planning, reverse dependency order, verification
│   │   ├── adapters/     #   A: postgres/ github/ notion/  (real undo)
│   │   ├── holds/        #   B: email + payments held channels, approval state machine
│   │   └── classify/     #   B: reversibility classifier client (rules fallback)
│   ├── migrations/       #   A: rewind-db schema
│   └── tests/
├── approvals-photon/     # B: TypeScript, spectrum-ts iMessage approvals service
├── dashboard/            # B: React + Vite. All UI lives here and only here.
├── classifier/           # B: dataset builder (APIs.guru), Veri training configs, eval
├── fetch-agent/          # A: uAgents Guardian + worker agents (ASI:One)
├── demo/                 # A: seeded fake company, rogue agent, reset, checksums
├── infra/                # A: docker-compose DB init, deploy (Vultr)
├── spikes/               # Phase 0 throwaway de-risk scripts
├── docs/
└── .github/workflows/    # CI
```

**A** = Stream A (Core and Undo). **B** = Stream B (Holds, Approvals, Classifier, all UI). See the ownership rules in [CONTRIBUTING.md](CONTRIBUTING.md).

## Getting started

```bash
cp .env.example .env   # fill in sandbox/test credentials only
```

Run instructions get added in Phase 1, along with `docker-compose.yml` and the `Makefile`.
