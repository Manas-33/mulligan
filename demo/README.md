# demo/ — Stream A

- `seed/`: the deterministic fake company "Acme Widgets" (prod-db, GitHub issues, Notion pricing doc)
- `scripts/`: `rogue_agent.py` (`--live` runs a real LLM agent over MCP and records its calls; `--replay <file>` sends a recording's calls deterministically and is the demo fallback), `teammate_edit.py` (human edits written straight to Postgres and Notion, on fields the agent also changed, so the rewind keeps and flags them), `reset`
- `recordings/`: recorded live runs. One known-good recording is committed as the default replay.
- `upstream/`: a small mock Slack MCP server whose tools Mulligan has never seen, for the rules gate demo
- `checksums/`: canonical per-system hashes that prove the restore
