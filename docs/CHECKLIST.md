# Checklist — setup, evidence, submission, cleanup

## Person A — core and undo (gateway, adapters, demo, Fetch, infra)

**Tonight, before coding**
- [ ] **GitHub:** a throwaway org and test repo, plus a fine-grained token with Issues and Contents write access. Use an account nobody watches, so closing 40 issues doesn't email real people.
- [ ] **Notion:** a test workspace and an internal integration. Share the pricing page with the integration through the page's Connections menu, or the API returns "not found". Copy the page ID.
- [ ] **Fetch:** Agentverse and ASI:One accounts, redeem the code(s), get the ASI:One API key and generate the agent seeds.
  - A is the team lead in the Submission Agent: create the team, get the Team ID and send it to B.
- [ ] **Devin:**
  - Whoever is org admin adds Mulligan as a custom MCP server (needs a public URL, such as a cloudflared tunnel).
  - 7 PM Cognition workshop: ask how to get the free team credits.
- [ ] **LLM key for the live rogue agent:** pick the model (open question 5) and fill in `ROGUE_AGENT_API_KEY`.

**While building**
- [ ] Note which pieces Devin built and save the session links for the Cognition judges.
- [ ] Commit one known-good rogue-agent recording as the replay fallback.

**Sunday, before 12 PM PT**
- [ ] README: run instructions, every agent's name and address, and both Innovation Lab badges.
- [ ] Fetch demo video, 3–5 minutes, showing the whole workflow inside ASI:One chat.
- [ ] Submit through the ASI:One Submission Agent (separate from Devpost).
- [ ] Final reset and replay run on the demo laptop.

## Person B — approvals, classifier, all UI

**Tonight, before coding**
- [ ] **Photon:**
  - At photon.codes/beta, create a project, connect iMessage, and copy the project ID and secret.
  - Choose a cloud line or the local provider on a Mac (open question 4).
  - The voice stretch needs an SMS number with SIP to ElevenLabs, which also counts toward the MLH ElevenLabs prize.
- [ ] **Sentry:**
  - The free plan allows one user, so B owns the account (B also owns the Sentry spike).
  - Create 3 projects (gateway, dashboard, Photon) and send A the gateway DSN.
  - Turn on Session Replay and Logs.
- [ ] **Stripe:** a test or sandbox account and an `sk_test_` key.
- [ ] **Email:** decide between Mailpit (no account needed) and Resend test mode (open question 3).
- [ ] **Veri:** DM @daniung on Discord for credits and redeem them before the first job (jobs on a $0 balance are stopped). Also a Hugging Face account and token, to push merged weights.
- [ ] **Paper:** redeem `LAHACKS26`, install the desktop app and connect its MCP.
- [ ] **StepFun:**
  - Get an API key from platform.stepfun.ai.
  - 8 PM workshop: ask whether calling it through Vercel AI Gateway counts.
- [ ] **Visa:** ask their mentors which Visa tools they expect.

**While building (judges ask for this)**
- [ ] **Sentry story:** screenshot a real trace, replay or log that led to a fix, and note the commit.
- [ ] Veri eval report: confusion matrix and unsafe-miss count.
- [ ] The Paper file link plus screenshots. Ask Paper how they judge, since their page lists no submission format.
- [ ] StepFun write-up: what each model does alongside Step 5 Preview, which their rules require.

**Sunday, before 12 PM PT**
- [ ] Assemble the Devpost entry:
  - A writes the Fetch and Devin sections; B writes the rest.
  - Select every sponsor track. Creao says unselected tracks aren't judged, and the others likely work the same way.
  - Pick Applied Intelligence as the main track.
- [ ] Approval phone logistics: either hand the judge a teammate's phone, or have the judge text Mulligan's iMessage number first. Don't text a judge's number without asking.

## Both

- [ ] Join the LA Hacks Discord and make sure both people are on the Devpost project.
- [ ] Fill in `.env` from `.env.example` and share secrets through a password manager, never Discord or git.
- [ ] Settle open questions 1–5 in [PLAN.md](../PLAN.md). Question 6 is settled: the repo is already renamed.
- [ ] **Pitch prep:** a 2-minute table pitch for Round 1 and the stage demo for Round 2. Have answers ready for:
  - "How is this different from Rubrik Agent Rewind?"
  - "Why not just use backups?"
  - "What if the model mislabels a tool?"
- [ ] **Demo kit:** a phone hotspot in case the venue Wi-Fi fails, chargers, and `make demo-reset` between judges.
- [ ] **After the event:**
  - Cancel Paper Pro before it renews, or email proof of enrollment for the free year.
  - Check whether the Fetch month auto-renews.
  - Revoke the GitHub, Notion and Stripe keys.
