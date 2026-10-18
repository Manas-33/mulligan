"""Stream B's hold manager: in-memory store, bus and runner, and a fake Photon service."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import BaseModel

from mulligan import holds, services
from mulligan.core import utcnow
from mulligan.holds.manager import PhotonHoldManager
from mulligan.holds.notify import NotifyOutcome, PhotonNotifier, approval_summary
from mulligan.interfaces import HoldDeps, HoldNotFound, NotHeld, WrongCode
from mulligan.models import (
    Action,
    ActionStatus,
    Agent,
    AgentKind,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalDecisionRequest,
    EventName,
    HoldKind,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    StepResult,
    System,
)

AGENT = Agent(id="claude_code", kind=AgentKind.claude_code, display_name="Claude Code")


def make_action(n: int = 1, tool: str = "email_send") -> Action:
    return Action(
        id=f"act_01M561R2302EME3JAF7RBZWM{n:02d}",
        session_id="ses_01M561Q6R04CJZXHXRCFFNM7C4",
        agent=AGENT,
        system=System.email,
        tool=tool,
        upstream_server=None,
        args_redacted={"to": "all-customers", "subject": "Price update"},
        status=ActionStatus.pending,
        reversibility=Reversibility(
            label=ReversibilityLabel.irreversible, source=ReversibilitySource.policy, reason=None
        ),
        summary=None,
        error=None,
        before_ref=None,
        after_ref=None,
        after_hashes={},
        inverse=None,
        approval=None,
        hold=None,
        explanation=None,
        sentry_trace_id=None,
        created_at=utcnow(),
        executed_at=None,
        reverted_at=None,
    )


class MemStore:
    def __init__(self) -> None:
        self.actions: dict[str, Action] = {}

    async def get(self, action_id: str) -> Action | None:
        return self.actions.get(action_id)

    async def save(self, action: Action) -> None:
        self.actions[action.id] = action

    async def list_by_status(self, *statuses: str) -> list[Action]:
        return [a for a in self.actions.values() if a.status.value in statuses]


class MemBus:
    def __init__(self) -> None:
        self.events: list[tuple[EventName, Any]] = []

    async def publish(self, event: EventName, data: BaseModel) -> int:
        self.events.append((event, data))
        return len(self.events)

    def names(self) -> list[str]:
        return [e.value for e, _ in self.events]


class Runner:
    def __init__(self, store: MemStore) -> None:
        self.store = store
        self.ran: list[str] = []

    async def run_held(self, action_id: str) -> Action:
        self.ran.append(action_id)
        action = self.store.actions[action_id].model_copy(update={"status": ActionStatus.executed})
        await self.store.save(action)
        return action


class FakePhoton:
    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            {
                "path": request.url.path,
                "token": request.headers.get("x-mulligan-service-token"),
                "body": json.loads(request.content),
            }
        )
        if self.status == 409:
            return httpx.Response(409, json={"detail": "approver_not_reachable"})
        return httpx.Response(self.status, json={"message_id": "msg_1", "status": "queued"})


def manager(
    photon: FakePhoton | None = None, ttl: timedelta | None = timedelta(minutes=15), **notifier: Any
) -> tuple[PhotonHoldManager, MemStore, MemBus, Runner]:
    store, bus = MemStore(), MemBus()
    runner = Runner(store)
    n = PhotonNotifier(
        base_url=notifier.get("base_url", "http://photon"),
        service_token="svc",
        approver=notifier.get("approver", "approver@icloud.com"),
        transport=httpx.MockTransport(photon or FakePhoton()),
    )
    return (
        PhotonHoldManager(HoldDeps(store=store, bus=bus, runner=runner), n, ttl),
        store,
        bus,
        runner,
    )


def decision(code: str, d: ApprovalDecision = ApprovalDecision.approve) -> ApprovalDecisionRequest:
    return ApprovalDecisionRequest(
        decision=d, code=code, channel=ApprovalChannel.imessage, decided_by="imessage:approver"
    )


async def test_hold_assigns_code_publishes_and_texts_the_approver() -> None:
    photon = FakePhoton()
    hm, store, bus, _ = manager(photon)
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    await hm.wait_for_notifications()

    assert held.status is ActionStatus.held
    assert held.approval is not None and len(held.approval.code) == 4
    assert held.approval.expires_at is not None
    assert held.hold is not None and held.hold.kind is HoldKind.email_outbox
    assert store.actions[held.id] == held
    assert bus.names() == ["action.held"]
    [req] = photon.requests
    assert req["path"] == "/notify/approval" and req["token"] == "svc"
    assert req["body"]["code"] == held.approval.code
    assert req["body"]["to"] == "approver@icloud.com"
    assert req["body"]["summary"].startswith("Claude Code wants to run email_send")


async def test_codes_are_unique_among_held_actions() -> None:
    hm, *_ = manager()
    codes = {
        (await hm.hold(make_action(n), HoldKind.email_outbox)).approval.code  # type: ignore[union-attr]
        for n in range(1, 40)
    }
    assert len(codes) == 39


async def test_approve_runs_the_call() -> None:
    hm, store, bus, runner = manager()
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    assert held.approval is not None
    result = await hm.on_decision(held.id, decision(held.approval.code))
    assert runner.ran == [held.id]
    assert result.status is ActionStatus.executed
    assert bus.names() == ["action.held", "action.approved"]
    approval = store.actions[held.id].approval
    assert approval is not None and approval.channel is ApprovalChannel.imessage


async def test_reject_does_not_run() -> None:
    hm, _, bus, runner = manager()
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    assert held.approval is not None
    result = await hm.on_decision(held.id, decision(held.approval.code, ApprovalDecision.reject))
    assert result.status is ActionStatus.rejected and runner.ran == []
    assert bus.names() == ["action.held", "action.rejected"]


async def test_decision_errors() -> None:
    hm, *_ = manager()
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    assert held.approval is not None
    with pytest.raises(HoldNotFound):
        await hm.on_decision("act_missing", decision("0000"))
    wrong = "0000" if held.approval.code != "0000" else "0001"
    with pytest.raises(WrongCode):
        await hm.on_decision(held.id, decision(wrong))
    await hm.on_decision(held.id, decision(held.approval.code, ApprovalDecision.reject))
    with pytest.raises(NotHeld):
        await hm.on_decision(held.id, decision(held.approval.code))


async def test_unreachable_approver_keeps_the_action_held() -> None:
    hm, store, _, _ = manager(FakePhoton(status=409))
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    await hm.wait_for_notifications()
    assert store.actions[held.id].status is ActionStatus.held
    assert await hm.notifier.notify_approval(held) is NotifyOutcome.not_reachable


async def test_notifier_disabled_or_down_never_fails_the_hold() -> None:
    hm, store, _, _ = manager(base_url="")
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    await hm.wait_for_notifications()
    assert store.actions[held.id].status is ActionStatus.held
    assert await hm.notifier.notify_approval(held) is NotifyOutcome.disabled

    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    broken = PhotonNotifier(
        "http://photon", "svc", "+15555550123", transport=httpx.MockTransport(down)
    )
    assert await broken.notify_approval(held) is NotifyOutcome.failed


async def test_release_due_expires_only_overdue_holds() -> None:
    hm, store, bus, _ = manager(ttl=timedelta(minutes=15))
    fresh = await hm.hold(make_action(1), HoldKind.email_outbox)
    old = await hm.hold(make_action(2), HoldKind.email_outbox)
    assert old.approval is not None
    store.actions[old.id] = old.model_copy(
        update={
            "approval": old.approval.model_copy(
                update={"expires_at": utcnow() - timedelta(seconds=1)}
            )
        }
    )
    changed = await hm.release_due()
    assert [a.id for a in changed] == [old.id]
    assert store.actions[old.id].status is ActionStatus.expired
    assert store.actions[fresh.id].status is ActionStatus.held
    assert bus.names()[-1] == "hold.expired"


async def test_no_ttl_means_never_expires() -> None:
    hm, *_ = manager(ttl=None)
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    assert held.approval is not None and held.approval.expires_at is None
    assert await hm.release_due() == []


async def test_cancel_before_it_ran_and_after() -> None:
    hm, store, bus, _ = manager()
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    step = await hm.cancel(held)
    assert step.result is StepResult.skipped and step.detail == "Rejected while held. Nothing ran."
    assert store.actions[held.id].status is ActionStatus.rejected
    assert bus.names()[-1] == "action.rejected"

    ran = make_action(2).model_copy(update={"status": ActionStatus.executed})
    await store.save(ran)
    step = await hm.cancel(ran)
    assert step.detail == "Action is executed; nothing to cancel."


def test_summary_is_short_and_readable() -> None:
    action = make_action().model_copy(update={"summary": "x" * 500})
    text = approval_summary(action)
    assert len(text) == 300 and text.endswith("…")
    assert approval_summary(make_action()) == (
        "Claude Code wants to run email_send: to=all-customers, subject=Price update"
    )


def test_core_picks_up_stream_b_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHOTON_SERVICE_URL", "http://photon")
    store = MemStore()
    hm = services.make_hold_manager(HoldDeps(store=store, bus=MemBus(), runner=Runner(store)))
    assert isinstance(hm, PhotonHoldManager)
    adapters = services.make_hold_adapters()
    assert [type(a).__name__ for a in adapters] == ["PaymentsAdapter"]
    assert adapters == holds.create_adapters()  # one shared instance


def test_notifier_reads_env_overrides_and_handle_fallback() -> None:
    n = PhotonNotifier.from_env(
        {
            "PHOTON_SERVICE_URL": "http://photon:8100/",
            "MULLIGAN_SERVICE_TOKEN": "svc",
            "APPROVER_HANDLE": " ",
            "APPROVER_PHONE": "+15555550123",
        }
    )
    assert (n.base_url, n.approver, n.enabled) == ("http://photon:8100", "+15555550123", True)
    assert not PhotonNotifier.from_env({}).enabled


async def test_approve_when_running_raises_marks_failed_instead_of_500() -> None:
    hm, store, bus, runner = manager()

    async def boom(action_id: str) -> Action:
        raise RuntimeError("ToolNotFound('email_send')")

    runner.run_held = boom  # type: ignore[method-assign]
    held = await hm.hold(make_action(), HoldKind.email_outbox)
    assert held.approval is not None
    result = await hm.on_decision(held.id, decision(held.approval.code))
    assert result.status is ActionStatus.failed
    assert result.error == "RuntimeError: ToolNotFound('email_send')"
    assert store.actions[held.id].status is ActionStatus.failed
    assert bus.names() == ["action.held", "action.approved", "action.failed"]
