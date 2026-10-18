"""Stream B's payments adapter and the hold manager's capture, with a fake Stripe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mulligan.core import utcnow
from mulligan.holds.manager import PhotonHoldManager
from mulligan.holds.notify import PhotonNotifier
from mulligan.holds.payments import (
    CANCEL,
    REFUND,
    Authorization,
    PaymentsAdapter,
    PaymentsUnavailable,
    StripeTestOps,
    release_time,
)
from mulligan.interfaces import Capture, HoldDeps, ToolCall
from mulligan.models import (
    Action,
    ActionStatus,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalDecisionRequest,
    HoldKind,
    StepResult,
    System,
)
from tests.test_holds import AGENT, MemBus, MemStore, make_action

AUTH_EXPIRES = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)


class FakeStripe:
    def __init__(self) -> None:
        self.intents: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self.fail: str | None = None

    def authorize(
        self, action_id: str, amount_cents: int, currency: str, description: str
    ) -> Authorization:
        pi = f"pi_{action_id[-6:]}"
        self.intents[pi] = "requires_capture"
        self.calls.append(("authorize", pi))
        return Authorization(
            payment_intent=pi, capture_before=AUTH_EXPIRES, status="requires_capture"
        )

    def _do(self, op: str, pi: str, to: str) -> str:
        self.calls.append((op, pi))
        if self.fail == op:
            raise RuntimeError(f"{op} exploded")
        self.intents[pi] = to
        return to

    def status(self, pi: str) -> str:
        return self.intents[pi]

    def capture(self, pi: str) -> str:
        return self._do("capture", pi, "succeeded")

    def cancel(self, pi: str) -> str:
        return self._do("cancel", pi, "canceled")

    def refund(self, pi: str) -> str:
        self._do("refund", pi, self.intents[pi])
        return "succeeded"


def charge_call(action_id: str) -> ToolCall:
    return ToolCall(
        action_id=action_id,
        session_id="ses_01M561Q6R04CJZXHXRCFFNM7C4",
        agent=AGENT,
        tool="payments_charge",
        system=System.payments,
        args={"customer_id": "cus_42", "amount_cents": 4999, "description": "Pro plan, 1 year"},
    )


async def run_adapter(adapter: PaymentsAdapter, action_id: str) -> tuple[Any, Any]:
    call = charge_call(action_id)
    before = await adapter.capture_before(call)
    result = await adapter.execute(call, before)
    after = await adapter.capture_after(call, before, result)
    return result, await adapter.build_inverse(call, before, after, result)


def executed(action_id: str, inverse: Any) -> Action:
    return make_action(int(action_id[-2:]), tool="payments_charge").model_copy(
        update={"system": System.payments, "status": ActionStatus.executed, "inverse": inverse}
    )


async def test_execute_authorizes_and_builds_cancel_inverse() -> None:
    stripe = FakeStripe()
    result, inverse = await run_adapter(PaymentsAdapter(stripe), "act_01M561R2302EME3JAF7RBZWM01")
    assert result.summary == "Authorized 49.99 USD: Pro plan, 1 year"
    assert result.structured_content["payment_intent"] == "pi_BZWM01"
    assert inverse.kind == CANCEL
    assert inverse.payload["capture_before"] == AUTH_EXPIRES.isoformat()
    assert stripe.calls == [("authorize", "pi_BZWM01")]


async def test_revert_cancels_refunds_or_skips() -> None:
    stripe = FakeStripe()
    adapter = PaymentsAdapter(stripe)
    _, inv = await run_adapter(adapter, "act_01M561R2302EME3JAF7RBZWM01")
    action = executed("act_01M561R2302EME3JAF7RBZWM01", inv)

    assert (await adapter.plan_revert(action)).detail == "Cancel the 49.99 USD authorization."
    step = await adapter.revert(action)
    assert (step.result, step.detail) == (
        StepResult.restored,
        "Cancelled the 49.99 USD authorization.",
    )
    assert (await adapter.revert(action)).result is StepResult.skipped  # already cancelled

    _, inv2 = await run_adapter(adapter, "act_01M561R2302EME3JAF7RBZWM02")
    stripe.capture("pi_BZWM02")
    captured = executed("act_01M561R2302EME3JAF7RBZWM02", inv2.model_copy(update={"kind": REFUND}))
    assert (await adapter.plan_revert(captured)).detail == "Refund 49.99 USD (already captured)."
    step = await adapter.revert(captured)
    assert (step.result, step.detail) == (
        StepResult.compensated,
        "Refunded 49.99 USD (was captured).",
    )


async def test_revert_reports_stripe_errors_instead_of_raising() -> None:
    stripe = FakeStripe()
    adapter = PaymentsAdapter(stripe)
    _, inv = await run_adapter(adapter, "act_01M561R2302EME3JAF7RBZWM01")
    stripe.fail = "cancel"
    step = await adapter.revert(executed("act_01M561R2302EME3JAF7RBZWM01", inv))
    assert step.result is StepResult.failed and "cancel exploded" in (step.detail or "")


def test_release_time_never_outlives_the_authorization() -> None:
    now = datetime(2026, 10, 18, 0, 0, tzinfo=UTC)
    assert release_time(now, AUTH_EXPIRES, timedelta(minutes=10)) == now + timedelta(minutes=10)
    assert release_time(now, AUTH_EXPIRES, timedelta(days=30)) == AUTH_EXPIRES - timedelta(hours=1)
    assert release_time(now, now + timedelta(minutes=30), timedelta(minutes=10)) == now


def test_live_keys_are_refused() -> None:
    with pytest.raises(PaymentsUnavailable):
        StripeTestOps("sk_live_nope")
    with pytest.raises(PaymentsUnavailable):
        StripeTestOps("")


class AdapterRunner:
    """Runs held calls through the payments adapter, like the core's run_held."""

    def __init__(self, store: MemStore, adapter: PaymentsAdapter) -> None:
        self.store, self.adapter = store, adapter

    async def run_held(self, action_id: str) -> Action:
        result, inverse = await run_adapter(self.adapter, action_id)
        action = self.store.actions[action_id].model_copy(
            update={"status": ActionStatus.executed, "inverse": inverse, "summary": result.summary}
        )
        await self.store.save(action)
        return action


async def test_approve_records_hold_then_release_due_captures() -> None:
    stripe = FakeStripe()
    adapter = PaymentsAdapter(stripe)
    store, bus = MemStore(), MemBus()
    hm = PhotonHoldManager(
        HoldDeps(store=store, bus=bus, runner=AdapterRunner(store, adapter)),
        PhotonNotifier("", "", ""),
        payments=adapter,
        payment_window=timedelta(minutes=10),
    )
    pending = make_action(1, tool="payments_charge").model_copy(update={"system": System.payments})
    held = await hm.hold(pending, HoldKind.payment_auth)
    assert held.approval is not None
    ran = await hm.on_decision(
        held.id,
        ApprovalDecisionRequest(
            decision=ApprovalDecision.approve,
            code=held.approval.code,
            channel=ApprovalChannel.imessage,
            decided_by="imessage:approver",
        ),
    )
    assert ran.hold is not None
    assert ran.hold.external_id == "pi_BZWM01"
    assert ran.hold.authorization_expires_at == AUTH_EXPIRES
    assert ran.hold.release_at is not None and ran.hold.release_at < AUTH_EXPIRES
    assert bus.names() == ["action.held", "action.approved", "action.executed"]

    assert await hm.release_due() == []  # undo window still open
    store.actions[held.id] = ran.model_copy(
        update={"hold": ran.hold.model_copy(update={"release_at": utcnow() - timedelta(seconds=1)})}
    )
    [captured] = await hm.release_due()
    assert stripe.intents["pi_BZWM01"] == "succeeded"
    assert captured.inverse is not None and captured.inverse.kind == REFUND
    assert bus.names()[-1] == "hold.released"
    assert await hm.release_due() == []  # captured once only


async def test_failed_capture_is_retried_later() -> None:
    stripe = FakeStripe()
    adapter = PaymentsAdapter(stripe)
    store, bus = MemStore(), MemBus()
    hm = PhotonHoldManager(
        HoldDeps(store=store, bus=bus, runner=AdapterRunner(store, adapter)),
        PhotonNotifier("", "", ""),
        payments=adapter,
    )
    _, inv = await run_adapter(adapter, "act_01M561R2302EME3JAF7RBZWM01")
    action = executed("act_01M561R2302EME3JAF7RBZWM01", inv)
    from mulligan.models import Hold

    action = action.model_copy(
        update={
            "hold": Hold(
                kind=HoldKind.payment_auth,
                release_at=utcnow() - timedelta(seconds=1),
                authorization_expires_at=AUTH_EXPIRES,
                external_id="pi_BZWM01",
            )
        }
    )
    await store.save(action)
    stripe.fail = "capture"
    assert await hm.release_due() == []
    assert store.actions[action.id].inverse.kind == CANCEL  # type: ignore[union-attr]
    stripe.fail = None
    assert len(await hm.release_due()) == 1


_ = Capture  # imported for type hints in helpers


async def test_rewound_payment_is_never_captured() -> None:
    """Live-run finding: after a rewind cancels the authorization, the capture timer skips it."""
    stripe = FakeStripe()
    adapter = PaymentsAdapter(stripe)
    store, bus = MemStore(), MemBus()
    hm = PhotonHoldManager(
        HoldDeps(store=store, bus=bus, runner=AdapterRunner(store, adapter)),
        PhotonNotifier("", "", ""),
        payments=adapter,
    )
    from mulligan.models import Hold

    _, inv = await run_adapter(adapter, "act_01M561R2302EME3JAF7RBZWM01")
    action = executed("act_01M561R2302EME3JAF7RBZWM01", inv).model_copy(
        update={
            "hold": Hold(
                kind=HoldKind.payment_auth,
                release_at=utcnow() - timedelta(seconds=1),  # window already over
                authorization_expires_at=AUTH_EXPIRES,
                external_id="pi_BZWM01",
            )
        }
    )
    step = await adapter.revert(action)
    assert step.result is StepResult.restored
    await store.save(action.model_copy(update={"status": ActionStatus.reverted}))

    assert await hm.release_due() == []
    assert ("capture", "pi_BZWM01") not in stripe.calls
    assert stripe.intents["pi_BZWM01"] == "canceled"
