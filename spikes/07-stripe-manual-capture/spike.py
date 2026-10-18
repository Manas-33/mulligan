# /// script
# requires-python = "==3.12.*"
# dependencies = ["stripe==16.0.0"]
# ///
"""Spike 0.7: Stripe manual capture (authorize now, capture or cancel later).

Proves the payments hold works the way Mulligan needs it:
  A) authorize -> capture        (the undo window closed, money moves)
  B) authorize -> cancel         (the agent's session was rewound, nothing moves)
  C) idempotent create           (a retried action doesn't double-authorize;
                                  a changed retry with the same key is refused)
  D) wrong-state transitions fail (no capture after cancel, no cancel after capture)

Run from the repo root:  uv run spikes/07-stripe-manual-capture/spike.py
Reads STRIPE_SECRET_KEY from .env. Refuses anything that isn't an sk_test_ key.
"""

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import stripe
from stripe import StripeClient

ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.split("#", 1)[0].strip()
    return env


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))
    return ok


def authorize(client: StripeClient, action_id: str, amount: int) -> stripe.PaymentIntent:
    # In the gateway, action_id is the Mulligan action id, used as both the
    # idempotency key and metadata so a Stripe object always maps back to its action.
    return client.v1.payment_intents.create(
        {
            "amount": amount,
            "currency": "usd",
            "capture_method": "manual",
            "allowed_payment_method_types": ["card"],  # payment_method_types is rejected on new accounts
            "payment_method": "pm_card_visa",  # Stripe's test Visa
            "confirm": True,
            "description": f"Mulligan spike 0.7 ({action_id})",
            "metadata": {"mulligan_action_id": action_id, "spike": "0.7"},
        },
        options={"idempotency_key": f"mulligan-{action_id}"},
    )


def capture_before(client: StripeClient, pi: stripe.PaymentIntent) -> str:
    charge = client.v1.charges.retrieve(pi.latest_charge)
    ts = charge.payment_method_details.card.capture_before
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else "unknown"


def main() -> int:
    key = load_env(ROOT / ".env").get("STRIPE_SECRET_KEY", "")
    if not key.startswith("sk_test_"):
        print("STRIPE_SECRET_KEY must be an sk_test_ key. Refusing to run.")
        return 2

    client = StripeClient(key)
    results: list[bool] = []
    run = uuid.uuid4().hex[:8]

    print("A) authorize -> capture")
    a = authorize(client, f"act_spike_{run}_a", 4200)
    results.append(check("authorized", a.status == "requires_capture", f"{a.id} status={a.status}"))
    results.append(check("amount_capturable", a.amount_capturable == 4200, str(a.amount_capturable)))
    print(f"  capture_before (auth expiry): {capture_before(client, a)}")
    a = client.v1.payment_intents.capture(a.id)
    results.append(check("captured", a.status == "succeeded", f"status={a.status} received={a.amount_received}"))

    print("B) authorize -> cancel (rewind)")
    b = authorize(client, f"act_spike_{run}_b", 1999)
    results.append(check("authorized", b.status == "requires_capture", f"{b.id} status={b.status}"))
    b = client.v1.payment_intents.cancel(b.id, {"cancellation_reason": "requested_by_customer"})
    results.append(check("canceled", b.status == "canceled", f"status={b.status} received={b.amount_received}"))
    results.append(check("no money moved", b.amount_received == 0))

    print("C) idempotent create (retry of the same action)")
    action_c = f"act_spike_{run}_c"
    c1 = authorize(client, action_c, 500)
    c2 = authorize(client, action_c, 500)
    results.append(check("same PaymentIntent on retry", c1.id == c2.id, f"{c1.id} vs {c2.id}"))
    results.append(check("metadata maps back to action", c1.metadata["mulligan_action_id"] == action_c))
    try:
        authorize(client, action_c, 900)  # same action id, different amount
        results.append(check("changed retry rejected", False, "Stripe accepted a different amount"))
    except stripe.IdempotencyError as e:
        results.append(check("changed retry rejected", True, type(e).__name__))
    client.v1.payment_intents.cancel(c1.id)

    print("D) wrong-state transitions are rejected")
    try:
        client.v1.payment_intents.capture(b.id)
        results.append(check("capture after cancel rejected", False, "capture unexpectedly succeeded"))
    except stripe.InvalidRequestError as e:
        results.append(check("capture after cancel rejected", True, e.code or type(e).__name__))
    try:
        client.v1.payment_intents.cancel(a.id)
        results.append(check("cancel after capture rejected", False, "cancel unexpectedly succeeded"))
    except stripe.InvalidRequestError as e:
        results.append(check("cancel after capture rejected", True, e.code or type(e).__name__))

    passed = all(results)
    print(f"\nSpike 0.7: {'PASS' if passed else 'FAIL'} ({sum(results)}/{len(results)} checks), stripe {stripe.VERSION}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
