# Mulligan: a do-over for AI agents

**One-liner:** Agents can run anything. Mulligan lets you undo everything they did.

## The problem
AI agents now take real actions: they write to production databases, close tickets, edit docs, send emails and move money. When one goes wrong, cleanup is manual and often impossible. In July 2025, Replit's coding agent deleted a company's production database during a code freeze.

## The idea
Mulligan is the layer agents act through. Agents connect to Mulligan as an MCP server, and Mulligan holds the real credentials, so agents can't go around it. Agents get typed tools (`pg_delete_rows`, `pg_update_rows`, `pg_drop_table`, `github_close_issue` and so on), never raw SQL, so Mulligan knows exactly what each call will change before it runs.

- **Records every action** with its before-state and how to reverse it. A dropped table isn't really dropped: Mulligan moves it to a hidden shadow table, so undoing the drop is just moving it back.
- **Undoes across systems at once.** One button rewinds an agent's session (or "rewind to 12:04") across every system, newest action first, and verifies each restore.
- **Respects human edits.** Mulligan stores a hash of every field the agent changed. If a person changed that field afterwards, the hash no longer matches, so Mulligan leaves their change alone and flags it instead of overwriting it.
- **Holds what can't be undone.** Every built-in tool has a fixed label in a policy table: reversible, reversible-but-noisy (it already notified people), compensable (can be offset) or irreversible. Irreversible actions, like emailing customers or moving money, wait for approval over iMessage. Payments are authorized now and only captured after the undo window closes.
- **Gates tools it has never seen.** Mulligan can also sit in front of other MCP servers. It has no undo adapter for their tools and no policy entry, so a simple rules check reads each tool's name, description and MCP annotations and labels it. A call goes through only if the tool clearly just reads. Everything else waits for approval, with the reason shown. The rules never override the policy table.

**Systems covered:** Postgres (production DB), GitHub (issues, comments, files) and Notion (pages). Email and payments are held channels. Tools from other MCP servers behind Mulligan are logged and gated, but can't be undone.

**Demo moment:** A cleanup agent goes rogue. It deletes customers, drops a table, closes 40 issues and rewrites the pricing doc, and a judge's phone buzzes asking to approve an email to every customer. Press Rewind and every system goes back. A teammate who fixed a price in the doc after the agent keeps their fix, flagged on the dashboard. Checksums prove everything else is exactly as it was.

**Demo safety:** The rogue agent runs live. If the venue network or the model misbehaves, one command replays a recorded run of the same agent instead, and the dashboard labels it as a replay.

**Pitch close:** "Backups restore a database. Mulligan undoes an agent."

## Tracks
- **Applied Intelligence** (primary)
- **Agentverse** (via the Fetch.ai agent below)

## Sponsors to address

| Sponsor | What they require | How Mulligan addresses it |
|---|---|---|
| **Sentry** | Use 2+ products beyond error monitoring; show how the data shaped the build | Tracing on every action and undo; MCP server monitoring; AI agent monitoring; Logs; Session Replay on the dashboard. Every timeline row links to its Sentry trace. |
| **Cognition (Devin)** | Build with Devin; judged on creativity, novelty, polish | Use Devin to build parts of Mulligan, and connect Devin to Mulligan as a protected agent via custom MCP. |
| **Fetch.ai** | Agent on Agentverse with Chat Protocol, usable in ASI:One; the main workflow happens inside ASI:One chat; bonus for multi-agent and Payment Protocol | A Guardian agent in ASI:One runs a protected task through a worker agent, shows what happened and what's held, and undoes on "rewind" in chat. Optional fee per protected session via Payment Protocol. |
| **Photon** | Built on Photon 2.0, live on a Photon channel (iMessage prize; separate voice prize) | iMessage approvals for held actions ("Reply YES 4821") and post-rewind summaries. Stretch: a voice call for the riskiest actions. |
| **Visa** | AI that improves shopping/payments, weighted on trust & responsibility | Agent purchases with an undo window: authorize now, capture later, cancel on rewind. Confirm with Visa mentors which Visa tools they expect. |
| **Paper** | Use Paper (desktop app + MCP); judged on design craft that doesn't look AI-generated | Design the Mulligan dashboard in Paper. |
| **StepFun** (optional) | App calls `step-5-preview` at runtime | Use Step 5 Preview to write the plain-English explanation of each action. |

Not targeted: veri.studio (a trained model would only gate the rare upstream tool, which simple rules already hold safely, so it wouldn't be a strong entry), Creao (its rule is that an agent built on agent.creao.ai does the project's core work, and Mulligan's core is its own gateway), Intuit, Workday, Base44, and the hardware challenges (QNX, Espressif, Even Realities).
