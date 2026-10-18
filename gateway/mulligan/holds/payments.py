"""The payments adapter: `payments_charge` with an undo window (Stripe test mode, manual capture).

`payments_charge` is always held. On approval the core runs it here, which *authorizes* the card
(nothing is collected yet). The hold manager then records the PaymentIntent on `action.hold` and
captures it when `release_at` passes; `release_at` is the end of the undo window and always
before Stripe's `capture_before`, or the capture would fail.

Rewind:
* not captured yet -> cancel the authorization (`payments.cancel_authorization`, ``restored``)
* captured         -> refund it (`payments.refund`, ``compensated``)

Findings from spike 0.7 (docs/SDK_NOTES.md): `allowed_payment_method_types` (not
`payment_method_types`), the Mulligan action id as the idempotency key, and
`payment_intent_unexpected_state` for calls in the wrong state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol

from mulligan.holds.notify import holds_env
from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult
from mulligan.models import Action, Inverse, RewindStep, StepResult, System

CANCEL = "payments.cancel_authorization"
REFUND = "payments.refund"
DEFAULT_UNDO_WINDOW = timedelta(minutes=10)
CAPTURE_MARGIN = timedelta(hours=1)
"""Capture at least this long before the authorization lapses."""


class PaymentsUnavailable(RuntimeError):
    """No test-mode Stripe key configured."""


@dataclass(frozen=True)
class Authorization:
    payment_intent: str
    capture_before: datetime
    status: str


class StripeOps(Protocol):
    """The Stripe calls the adapter makes. Fakeable in tests."""

    def authorize(
        self, action_id: str, amount_cents: int, currency: str, description: str
    ) -> Authorization: ...
    def status(self, payment_intent: str) -> str: ...
    def capture(self, payment_intent: str) -> str: ...
    def cancel(self, payment_intent: str) -> str: ...
    def refund(self, payment_intent: str) -> str: ...


class StripeTestOps:
    """Real Stripe, test mode only (refuses anything but an sk_test_ key)."""

    def __init__(self, secret_key: str) -> None:
        if not secret_key.startswith("sk_test_"):
            raise PaymentsUnavailable("STRIPE_SECRET_KEY must be an sk_test_ key")
        from stripe import StripeClient  # imported lazily: the gateway runs without payments too

        self.client = StripeClient(secret_key)

    def authorize(
        self, action_id: str, amount_cents: int, currency: str, description: str
    ) -> Authorization:
        pi = self.client.v1.payment_intents.create(
            {
                "amount": amount_cents,
                "currency": currency,
                "capture_method": "manual",
                "allowed_payment_method_types": ["card"],
                "payment_method": "pm_card_visa",  # Stripe's test Visa
                "confirm": True,
                "description": description[:500],
                "metadata": {"mulligan_action_id": action_id},
            },
            options={"idempotency_key": f"mulligan-{action_id}"},
        )
        charge = self.client.v1.charges.retrieve(str(pi.latest_charge))
        details = charge.payment_method_details
        capture_before = details.card.capture_before if details and details.card else None
        when = (
            datetime.fromtimestamp(capture_before, UTC)
            if capture_before
            else datetime.now(UTC) + timedelta(days=6)  # Stripe's floor for card auths is ~7 days
        )
        return Authorization(payment_intent=pi.id, capture_before=when, status=pi.status)

    def status(self, payment_intent: str) -> str:
        return str(self.client.v1.payment_intents.retrieve(payment_intent).status)

    def capture(self, payment_intent: str) -> str:
        return str(self.client.v1.payment_intents.capture(payment_intent).status)

    def cancel(self, payment_intent: str) -> str:
        return str(self.client.v1.payment_intents.cancel(payment_intent).status)

    def refund(self, payment_intent: str) -> str:
        return str(self.client.v1.refunds.create({"payment_intent": payment_intent}).status)


def undo_window() -> timedelta:
    raw = holds_env().get("MULLIGAN_PAYMENT_UNDO_MINUTES", "").strip()
    return timedelta(minutes=float(raw)) if raw else DEFAULT_UNDO_WINDOW


def release_time(now: datetime, capture_before: datetime, window: timedelta) -> datetime:
    """End of the undo window, but never later than CAPTURE_MARGIN before the auth lapses."""
    latest = capture_before - CAPTURE_MARGIN
    return min(now + window, latest) if latest > now else now


def _step(action: Action, result: StepResult, detail: str) -> RewindStep:
    return RewindStep(
        action_id=action.id,
        system=action.system,
        tool=action.tool,
        result=result,
        kept_fields=[],
        changes=[],
        changes_truncated=False,
        detail=detail,
    )


def _dollars(cents: int, currency: str) -> str:
    return f"{cents / 100:,.2f} {currency.upper()}"


class PaymentsAdapter(Adapter):
    system: ClassVar[System] = System.payments
    tools: ClassVar[frozenset[str]] = frozenset({"payments_charge"})

    def __init__(self, ops: StripeOps | None = None) -> None:
        self._ops = ops

    @property
    def ops(self) -> StripeOps:
        if self._ops is None:
            self._ops = StripeTestOps(holds_env().get("STRIPE_SECRET_KEY", "").strip())
        return self._ops

    async def _run(self, fn: Any, *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)  # the Stripe SDK is synchronous

    async def capture_before(self, call: ToolCall) -> Capture:
        return Capture(ref=None)

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        amount = int(call.args["amount_cents"])
        currency = str(call.args.get("currency") or "usd").lower()
        description = str(call.args.get("description") or "")
        auth: Authorization = await self._run(
            self.ops.authorize, call.action_id, amount, currency, description
        )
        if auth.status != "requires_capture":
            raise RuntimeError(f"authorization not held (Stripe status {auth.status})")
        before.state.update(
            payment_intent=auth.payment_intent,
            capture_before=auth.capture_before.isoformat(),
            amount_cents=amount,
            currency=currency,
            customer_id=str(call.args.get("customer_id") or ""),
        )
        return ToolResult(
            structured_content={
                "payment_intent": auth.payment_intent,
                "authorized_cents": amount,
                "capture_before": auth.capture_before.isoformat(),
            },
            summary=f"Authorized {_dollars(amount, currency)}: {description}"[:300],
        )

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        return Capture(ref=f"stripe:{before.state['payment_intent']}")

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        return Inverse(kind=CANCEL, payload={k: before.state[k] for k in before.state})

    async def plan_revert(self, action: Action) -> RewindStep:
        if action.inverse is None:
            return _step(action, StepResult.skipped, "Nothing was authorized.")
        amount = _dollars(
            action.inverse.payload["amount_cents"], action.inverse.payload["currency"]
        )
        if action.inverse.kind == REFUND:
            return _step(action, StepResult.pending, f"Refund {amount} (already captured).")
        return _step(action, StepResult.pending, f"Cancel the {amount} authorization.")

    async def revert(self, action: Action) -> RewindStep:
        if action.inverse is None:
            return _step(action, StepResult.skipped, "Nothing was authorized.")
        pi = action.inverse.payload["payment_intent"]
        amount = _dollars(
            action.inverse.payload["amount_cents"], action.inverse.payload["currency"]
        )
        try:
            status = await self._run(self.ops.status, pi)
            if status == "requires_capture":
                await self._run(self.ops.cancel, pi)
                return _step(action, StepResult.restored, f"Cancelled the {amount} authorization.")
            if status == "succeeded":
                await self._run(self.ops.refund, pi)
                return _step(action, StepResult.compensated, f"Refunded {amount} (was captured).")
            if status == "canceled":
                return _step(action, StepResult.skipped, "Authorization already cancelled.")
            return _step(action, StepResult.failed, f"Unexpected Stripe status {status}.")
        except Exception as exc:  # noqa: BLE001 (revert reports failures, never raises)
            return _step(action, StepResult.failed, f"Stripe error: {exc}"[:300])

    async def capture(self, action: Action) -> str:
        """Collect the money (called by the hold manager when the undo window ends)."""
        assert action.inverse is not None
        return str(await self._run(self.ops.capture, action.inverse.payload["payment_intent"]))

    async def checksum(self) -> str | None:
        return None  # a held channel: nothing to verify by hash
