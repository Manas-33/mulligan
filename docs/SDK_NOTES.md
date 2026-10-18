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
