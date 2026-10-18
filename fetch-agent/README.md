# Mulligan Guardian

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Mulligan is a do-over for AI agents. Agents act through it instead of touching real systems: it records every change with its before-state, holds anything that can't be undone (emails, payments) until a person approves it, and can rewind a whole session across Postgres, GitHub and Notion, keeping any edit a person made since.

Mulligan Guardian brings that to ASI:One chat:

1. **Give it a task**, for example *"do the quarter-end cleanup"*. Its worker agent runs the task through Mulligan: it removes inactive customers, zeroes the Widget prices, closes stale GitHub issues, rewrites a price in the Notion pricing doc and emails customers.
2. **It reports what happened**: every change Mulligan recorded, and what Mulligan is holding for approval (the email). Each held action comes as a card you can approve or reject right in chat.
3. **Say "rewind"** (or press **Rewind** on the card) and Mulligan undoes the whole session, newest change first. It reports which systems were restored, any edits a person made afterwards that it kept, and whether each system's checksum matches.

Other commands: `status`, `approve 1234` / `reject 1234` (a held action's code), `help`.

## Agents

| Agent | Address | Role |
|---|---|---|
| `mulligan-guardian` | `agent1qveg9ca3jn06upx2ztx78k40kjm4jxg2ywuul2lmfrvc77druqwp6qae9w4` | Chats in ASI:One (Agent Chat Protocol, mailbox), hands tasks to the worker, approves and rejects held actions, rewinds sessions |
| `mulligan-worker` | `agent1q2hqgd60cdsfxhda3tyesj0zmyca4mle66c6varrj6gmzrfhp88aqsrdngz` | Runs tasks only through Mulligan's MCP tools, as Mulligan's `fetch` agent |

Guardian profile: https://agentverse.ai/agents/details/agent1qveg9ca3jn06upx2ztx78k40kjm4jxg2ywuul2lmfrvc77druqwp6qae9w4/profile

## Running them

Needs the Mulligan gateway running (see the repo README) and, in the repo's `.env`: `GUARDIAN_AGENT_SEED`, `WORKER_AGENT_SEED`, `MULLIGAN_SERVICE_TOKEN` and a `fetch:<token>` entry in `MULLIGAN_AGENT_TOKENS`.

```bash
cd fetch-agent
uv sync
uv run python -m mulligan_fetch.worker     # port 8011
uv run python -m mulligan_fetch.guardian   # port 8010, mailbox
```

The Guardian's mailbox is connected once through the Inspector link it prints at startup (Connect → Mailbox). In ASI:One, open the Guardian's profile and use **Chat with Agent**, or mention it with `@agent1qveg9…`.

```bash
uv run pytest                              # unit tests, no network
```
