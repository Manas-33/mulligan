# SDK notes

Check each SDK against its current docs before you use it. Record the pinned version, the docs link, the date you checked, and any gotchas. Add entries below; don't rewrite old ones.

Template:

```
## <SDK> — <pinned version>
Docs: <url>  ·  Checked: YYYY-MM-DD by <A|B>
- Gotcha / decision
```

## Docs index
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Claude Code MCP: https://code.claude.com/docs/en/mcp · hooks: https://code.claude.com/docs/en/hooks
- Devin custom MCP: https://docs.devin.ai/work-with-devin/mcp
- Fetch ASI:One agent: https://uagents.fetch.ai/docs/examples/asi-1
- Photon spectrum-ts: https://photon.codes/docs/spectrum-ts/introduction
- Sentry MCP monitoring (Python): https://docs.sentry.io/platforms/python/tracing/instrumentation/custom-instrumentation/mcp-module/

## Entries

_(none yet. Phase 0 fills these in)_

## uAgents + Agent Chat Protocol (spike 0.5): uagents 0.26.1, uagents-core 0.4.12
Docs: https://uagents.fetch.ai/docs/examples/asi-1 · cards: https://docs.agentverse.ai/documentation/advanced-usages/agent-driven-interactive-cards and https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards · card playground: https://asi1.ai/developer/card-playground · README rules: https://innovationlab.fetch.ai/events/hackathons/la-hacks-2026/hackpack · Checked: 2026-10-17 by A
Result: **pass.** ASI:One chat gets a reply, and Agentverse search finds the agent by name. Spike code: `spikes/05-fetch-agent/` (`agent.py`, `client.py`).
- Pins: `uagents==0.26.1`, `uagents-core==0.4.12`, `openai==3.35.0`, Python 3.12. The chat protocol is `AgentChatProtocol` 0.3.0, imported from `uagents_core.contrib.protocols.chat`.
- Guardian address, derived from `GUARDIAN_AGENT_SEED`: `agent1qveg9ca3jn06upx2ztx78k40kjm4jxg2ywuul2lmfrvc77druqwp6qae9w4`. The address is fixed by the seed, so the real Guardian keeps it as long as it uses the same seed. Profile: https://agentverse.ai/agents/details/agent1qveg9ca3jn06upx2ztx78k40kjm4jxg2ywuul2lmfrvc77druqwp6qae9w4/profile
- Mailbox setup is a one-time step per address. Run the agent, open the Inspector link it prints (in Chrome, logged into agentverse.ai), click **Connect** and choose **Mailbox**. The log then says `Successfully registered as mailbox agent in Agentverse`. Later restarts stay connected. The Inspector's "Check your agent" page is a generic checklist, not an error.
- Ignore the startup warning "not enough funds to register on Almanac contract". Registration on the Almanac API is enough for mailbox routing and for ASI:One.
- `readme_path=` and `description=` on `Agent(...)` publish the README and the description to Agentverse on every start, so no manual profile editing is needed. The agent showed up in `POST https://agentverse.ai/v1/search/agents` about 15 minutes after first registration. The listing showed `category: community`, so whether the badges move it to Innovation Lab is still unconfirmed.
- Hackathon README (LA Hacks hackpack): list each agent's name and address, link any extra resources, and include both badges, `![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)` and `![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)`. Track 1 deliverables also need an ASI:One chat session URL and the Agentverse profile URL.
- In ASI:One, a message without an @mention goes to the user's personal AI and never reaches the agent; the first try got no reply for that reason. Use `@agent1… text`, or **Chat with Agent** on the profile page, which fills in the mention. ASI:One sends from its own relay agent, `agent1q2sq5ac5pnw0d3nje6u4n802sd6tmlh98580v6rp2ymhlvnv63fuchg40np`.
- The forwarded text keeps the `@agent1…` mention, so strip it before matching commands such as "rewind": `re.sub(r"@agent1[0-9a-z]+", "", text)`.
- Messages from ASI:One arrived as a single `TextContent`, with no `StartSessionContent`. The agent should send a `ChatAcknowledgement` first, then a `ChatMessage`. Ending the reply with `EndSessionContent` works fine.
- **Interactive Cards work from uAgents.** Send one `ChatMessage` with a `TextContent` (the narration) and a `MetadataContent(metadata={...})`, where every metadata value is a string: `card_protocol_version: "1"`, `requires_card_interaction: "true"`, `card_kind` (`review`, `detail`, `form`, `carousel` or `custom`), and `card_payload` as a JSON string. A review card has `title`, `summary_rows: [{label, value}]`, `approve_cta: {label, selection, primary}` and `reject_cta: {label, selection}`.
- Each card `selection` **must be an object** (for example `{"action": "rewind", "session_id": "…"}`). With string selections, ASI:One silently shows only the narration text.
- A click comes back as a new chat message whose text is the selection plus `approved`, serialized as JSON, for example `{"action":"rewind","session_id":"sess_demo","approved":true}`. That was from a direct @mention chat; planner-routed chats reportedly send prose instead, so try `json.loads()` first and fall back to text parsing.
- The ASI:One API at `https://api.asi1.ai/v1` (OpenAI client, `asi1-mini`) worked for wording replies, at roughly 1 to 2 s per call.
- Local testing without the browser: `client.py` uses a throwaway seed and a local endpoint, sends one `ChatMessage` through the mailbox, and logs the ack and the reply.
