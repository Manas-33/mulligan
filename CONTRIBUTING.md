# Working together

Two people, each running a separate Claude Code session. The full process is in [PLAN.md](PLAN.md). This page covers the rules that stop us from colliding.

## Ownership

| Path | Owner |
|---|---|
| `contracts/` | **Shared.** Change only through a PR that both people approve. |
| `gateway/mulligan/{api,mcp_server,log,orchestrator,adapters}/`, `gateway/migrations/`, `gateway/mulligan/app.py`, `gateway/pyproject.toml` | Stream A |
| `gateway/mulligan/holds/`, `gateway/mulligan/classify/` | Stream B |
| `approvals-photon/`, `dashboard/`, `classifier/` | Stream B |
| `fetch-agent/`, `demo/`, `infra/`, `docker-compose.yml`, `Makefile`, `.github/workflows/` | Stream A |
| `spikes/<NN>-name/` | Whoever owns that spike in PLAN.md Phase 0 |
| `docs/SDK_NOTES.md` | Both. Append only, one section per SDK. |

- All UI is Stream B's. Stream A never adds UI code. It exposes data only through the REST API and SSE events in `contracts/`.
- Stream B's code inside `gateway/` plugs in through the `Adapter`, `HoldManager`, `Policy` and `Classifier` interfaces. To change a shared gateway file, open a PR and ask Stream A to review it.

## Git

- `main` is protected. Work on branches named `a/<phase>-<topic>` or `b/<phase>-<topic>` (for example `a/p2-postgres-undo`).
- Use one git worktree per Claude Code session so the two sessions never share a working tree.
- Keep PRs small. Rebase on `main` before merging. Never commit `.env` or real data.

## Phase rhythm

1. Paste the phase's `GOAL` block from PLAN.md into your session.
2. Run `/loop` with at least 3 passes: build (check docs first), verify (tests and acceptance), audit (`/code-review`, contract check, secrets check).
3. Stop at the phase's approval gate. Both people approve before the next phase starts.

## Safety

Use sandbox or test accounts only: a throwaway GitHub org, a test Notion workspace, Stripe `sk_test_` keys and a test iMessage line. Agents never receive credentials.
