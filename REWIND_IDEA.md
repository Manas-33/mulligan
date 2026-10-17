# Rewind: Ctrl+Z for AI agents

**One-liner:** Agents can run anything. Rewind lets you undo everything they did.

## The problem
AI agents now take real actions: they write to production databases, close tickets, edit docs, send emails and move money. When one goes wrong, cleanup is manual and often impossible. In July 2025, Replit's coding agent deleted a company's production database during a code freeze.

## The idea
Rewind is the layer agents act through. Agents connect to Rewind as an MCP server, and Rewind holds the real credentials, so agents can't go around it.

- **Records every action** with its before-state and how to reverse it.
- **Undoes across systems at once.** One button rewinds an agent's session (or "rewind to 12:04") across every system in reverse dependency order, and verifies each restore.
- **Respects human edits.** If a person changed something after the agent did, Rewind merges instead of overwriting their work.
- **Holds what can't be undone.** A trained classifier labels each action as reversible, reversible-but-noisy (it already notified people), compensable (can be offset) or irreversible. Irreversible actions, like emailing customers or moving money, wait for approval over iMessage. Payments are authorized now and only captured after the undo window closes.

**Systems covered:** Postgres (production DB), GitHub (issues, comments, files) and Notion (pages). Email and payments are held channels.

**Demo moment:** A cleanup agent goes rogue. It deletes customers, drops a table, closes 40 issues and rewrites the pricing doc, and a judge's phone buzzes asking to approve an email to every customer. Press Rewind and every system goes back. A teammate's edit made after the agent survives. Checksums prove the restore.

**Pitch close:** "Backups restore a database. Rewind undoes an agent."

## Tracks
- **Applied Intelligence** (primary)
- **Agentverse** (via the Fetch.ai agent below)

## Sponsors to address

| Sponsor | What they require | How Rewind addresses it |
|---|---|---|
| **Sentry** | Use 2+ products beyond error monitoring; show how the data shaped the build | Tracing on every action and undo; MCP server monitoring; AI agent monitoring; Logs; Session Replay on the dashboard; Uptime on the gateway. Every timeline row links to its Sentry trace. |
| **Cognition (Devin)** | Build with Devin; judged on creativity, novelty, polish | Use Devin to build parts of Rewind, and connect Devin to Rewind as a protected agent via custom MCP. |
| **Fetch.ai** | Agent on Agentverse with Chat Protocol, usable in ASI:One; the main workflow happens inside ASI:One chat; bonus for multi-agent and Payment Protocol | A Guardian agent in ASI:One runs a protected task through a worker agent, shows what happened and what's held, and undoes on "rewind" in chat. Optional fee per protected session via Payment Protocol. |
| **Photon** | Built on Photon 2.0, live on a Photon channel (iMessage prize; separate voice prize) | iMessage approvals for held actions ("Reply YES 4821") and post-rewind summaries. Stretch: a voice call for the riskiest actions. |
| **Visa** | AI that improves shopping/payments, weighted on trust & responsibility | Agent purchases with an undo window: authorize now, capture later, cancel on rewind. Confirm with Visa mentors which Visa tools they expect. |
| **veri.studio** | Train an open-source model on their platform | Fine-tune the small reversibility classifier on Veri. |
| **Paper** | Use Paper (desktop app + MCP); judged on design craft that doesn't look AI-generated | Design the Rewind dashboard in Paper. |
| **MLH** | Per prize | Tiger Data for the action log, a GoDaddy domain, Vultr to host the gateway. |
| **StepFun** (optional) | App calls `step-5-preview` at runtime | Use Step 5 Preview to write the plain-English explanation of each action. |

Not targeted: Creao (its rule is that an agent built on agent.creao.ai does the project's core work, and Rewind's core is its own gateway), Intuit, Workday, Base44, and the hardware challenges (QNX, Espressif, Even Realities).
