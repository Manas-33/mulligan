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

## Stripe Python (`stripe`) — 16.0.0 · spike 0.7 PASS (11/11, twice in a row)
Docs: https://docs.stripe.com/payments/place-a-hold-on-a-payment-method · https://docs.stripe.com/api/payment_intents/capture?lang=python  ·  Checked: 2026-10-17 by B
Spike: `spikes/07-stripe-manual-capture/spike.py` (run with `uv run`, Python 3.12, test-mode key only)
- **Client style:** `StripeClient(key)` and `client.v1.payment_intents.create({...}, options={"idempotency_key": ...})`. Request params are a dict passed positionally. `capture(id)` and `cancel(id, {...})` follow the same pattern.
- **`payment_method_types` is rejected** on new accounts ("no longer supported"). Use `allowed_payment_method_types: ["card"]` to stay card-only. Server-side confirm uses `payment_method: "pm_card_visa"` with `confirm: True`.
- **Statuses:** after create and confirm → `requires_capture` with `amount_capturable` = amount. `capture` → `succeeded`. `cancel` → `canceled`, `amount_received` 0.
- **Authorization window:** read it from `charge.payment_method_details.card.capture_before`. The test Visa showed about 7 days. Docs: Visa customer-initiated 7 days, merchant-initiated about 4 days 18 hours. Uncaptured intents auto-cancel after 7 days. **So `hold.release_at` must come well before `capture_before`.**
- **Idempotency:** use the Mulligan `action_id` as the key (`mulligan-<action_id>`) and in `metadata.mulligan_action_id`. A retry with identical params returns the same PaymentIntent. A retry with the same key and **different params** raises `stripe.IdempotencyError`. The gateway should treat that as a bug, not retry it.
- **Wrong-state calls** raise `stripe.InvalidRequestError` with code `payment_intent_unexpected_state` (capture after cancel, cancel after capture). Rewind after capture can't cancel, so it has to refund instead: the action becomes `compensable`. This matches the plan.

## Photon 2.0 API (`@photon-ai/api`) — 0.5.0 · spike 0.6 PASS (approve 1.5 s, reject 2.2 s; budget 5 s)
Docs: https://photon.codes/docs/beta/get-started/imessage · https://photon.codes/docs/beta/api-client/credentials  ·  Checked: 2026-10-17 by B
Spike: `spikes/06-photon-imessage/` (`npm run spike`, parser tests: `npx tsx --test parse.test.ts`). Node 24+ required.
- **Use `@photon-ai/api`, not `spectrum-ts`.** Our dashboard is the Photon 2.0 beta (app.beta.photon.codes). `spectrum-ts` 12.x is the older "Stable" product and rejects `pho_prj_` project IDs (`422 VALIDATION_ERROR`). In 2.0, the project API key (`pho_sk_…`) goes in `Authorization: Bearer`, and only works under `/v1/projects/{projectId}`. Base URL is `https://api.photon.codes`.
- **Calls:** `new Photon({ headers: { Authorization: \`Bearer ${key}\` } })`, then `photon.projects.listMessages({ path: { id }, query })` and `photon.projects.sendMessage({ path: { id }, headers: { "idempotency-key": … }, body: { spaceId, senderId, content: { type: "text", format: "plain", text } } })`. Message items are typed loosely, so we cast them to a local type (`sentBy.handle`, `receivedBy.id`, `space.{id,kind}`, `content.{type,text}`, `createdAt`).
- **`listMessages` `direction` is `received` | `sent`.** `dispatched` is a message *status*, not a direction. Send returns status `dispatched` (accepted, not proof of delivery).
- **Shared lines can't text first** (`403 SHARED_LINE_INBOUND_REQUIRED`). The approver must text the line once, after their assignment. Then we send into that `space.id` with `senderId` = the inbound message's `receivedBy.id`. **Contract impact:** Mulligan has to keep each approver's `spaceId`/`senderId`, and the demo setup includes "approver texts the line once".
- **No threaded replies on shared lines:** `parentMessageId` gets `409 SPACE_NOT_SENDABLE` ("Replies are not supported for this Space"). Send confirmations as plain messages.
- **The approver must be on iMessage.** An Android phone receives our outbound text (as RCS), but its RCS reply never reaches the API. We tested with the Mac's Messages app signed in with an Apple ID email, added in the dashboard via "Use an iMessage email instead". That gives the email its own shared line. Handles can be an E.164 phone or an email, so we compare phones by digits and emails case-insensitively.
- **Polling** every 1 s with `createdSince` = send time and `order: asc` got no 429s. The docs' sample polls every 2 s and suggests live events (WebSocket) for long-running agents. That's worth a look in Phase 2. Shared lines also have plan seat limits per assigned handle.
- Lag is measured as local poll time minus the server's `createdAt`, so clock skew of a second or so is possible. That's fine for a 5 s budget.
