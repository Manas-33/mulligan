"""The call pipeline, holds and rewind against a real mulligan-db, with an in-memory adapter."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from mulligan.app import create_app
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    ActionStatus,
    Agent,
    AgentKind,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalDecisionRequest,
    RewindJobStatus,
    RewindRequest,
    RewindScope,
    SessionMode,
    SessionStatus,
    StepResult,
)
from mulligan.orchestrator.rewind import INTERRUPTED
from mulligan.services import Services, assemble
from mulligan.settings import Settings
from tests.fakes import FakeTableAdapter

pytestmark = pytest.mark.integration

AGENT = Agent(id="claude_code", kind=AgentKind.claude_code, display_name="Claude Code")
ROWS = {1: {"name": "Widget", "price": 49}, 2: {"name": "Gadget", "price": 99}}
SETTINGS = Settings(dashboard_token="dash", service_token="svc", demo=True)


@pytest.fixture
async def svc(log_pool: Any) -> Services:
    return assemble(
        SETTINGS, LogStore(log_pool), EventBus(log_pool), [FakeTableAdapter(ROWS)], pool=None
    )


def fake(svc: Services) -> FakeTableAdapter:
    adapter = svc.gateway.by_system[FakeTableAdapter.system]
    assert isinstance(adapter, FakeTableAdapter)
    return adapter


async def test_update_then_rewind_restores_and_verifies(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live, "mcp-1")
    before = await fake(svc).checksum()
    result = await svc.gateway.call_tool(
        session,
        "pg_update_rows",
        {
            "table": "products",
            "filter": [{"column": "id", "op": "eq", "value": 1}],
            "values": {"price": 0},
        },
    )
    assert result.structured_content["status"] == "executed"
    action = await svc.store.get(result.structured_content["action_id"])
    assert action is not None and action.status is ActionStatus.executed
    assert set(action.after_hashes) == {"postgres:public.products/1/price"}
    assert fake(svc).rows[1]["price"] == 0

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert plan.status is RewindJobStatus.planned
    assert [s.result for s in plan.steps] == [StepResult.pending]
    assert fake(svc).rows[1]["price"] == 0  # a dry run changes nothing

    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    assert job.status is RewindJobStatus.running
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None and done.status is RewindJobStatus.completed
    assert [s.result for s in done.steps] == [StepResult.restored]
    assert done.verification["postgres"].ok is True
    assert done.verification["postgres"].expected == before
    assert fake(svc).rows[1]["price"] == 49
    reverted = await svc.store.get(action.id)
    assert reverted is not None and reverted.status is ActionStatus.reverted
    session_after = await svc.store.get_session(session.id)
    assert session_after is not None and session_after.status is SessionStatus.rewound
    assert session_after.action_count == 1


async def test_human_edit_is_kept_and_checksum_accounts_for_it(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 0}})
    await svc.gateway.call_tool(
        session, "pg_delete_rows", {"filter": [{"column": "id", "op": "eq", "value": 2}]}
    )
    fake(svc).rows[1]["price"] = 45  # a teammate fixes the price by hand

    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None
    assert [s.tool for s in done.steps] == ["pg_delete_rows", "pg_update_rows"]  # newest first
    assert [s.result for s in done.steps] == [StepResult.restored, StepResult.kept_human_edit]
    assert done.steps[1].kept_fields == ["postgres:public.products/1/price"]
    assert fake(svc).rows == {
        1: {"name": "Widget", "price": 45},
        2: {"name": "Gadget", "price": 99},
    }
    assert done.verification["postgres"].ok is True
    assert done.status is RewindJobStatus.completed
    statuses = {a.tool: a.status for a in await svc.store.list_actions(session_id=session.id)}
    assert statuses == {
        "pg_update_rows": ActionStatus.conflict,
        "pg_delete_rows": ActionStatus.reverted,
    }


async def test_failed_step_makes_the_job_partial(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 1}})
    fake(svc).fail_next_revert = True
    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None and done.status is RewindJobStatus.partial
    assert done.steps[0].result is StepResult.failed
    action = await svc.store.get(done.steps[0].action_id)
    assert action is not None and action.status is ActionStatus.revert_failed


async def run_rewind(svc: Services, session_id: str) -> Any:
    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session_id), dry_run=False)
    )
    await svc.rewinder.wait()
    return await svc.store.get_job(job.id)


async def test_a_failed_step_is_tried_again_by_the_next_rewind(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 1}})
    fake(svc).fail_next_revert = True
    first = await run_rewind(svc, session.id)
    assert first.status is RewindJobStatus.partial
    assert fake(svc).rows[1]["price"] == 1

    again = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert [s.action_id for s in again.steps] == [first.steps[0].action_id]
    second = await run_rewind(svc, session.id)
    assert second.status is RewindJobStatus.completed
    assert [s.result for s in second.steps] == [StepResult.restored]
    assert fake(svc).rows[1]["price"] == 49
    action = await svc.store.get(second.steps[0].action_id)
    assert action is not None and action.status is ActionStatus.reverted
    assert action.error is None


async def test_an_action_left_reverting_is_tried_again(svc: Services) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    out = await svc.gateway.call_tool(
        session, "pg_update_rows", {"filter": flt, "values": {"price": 1}}
    )
    action = await svc.store.get(out.structured_content["action_id"])
    assert action is not None
    # The gateway stopped in the middle of reverting it.
    await svc.store.save(action.model_copy(update={"status": ActionStatus.reverting}))

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert [s.action_id for s in plan.steps] == [action.id]
    stuck = await svc.store.get(action.id)
    assert stuck is not None and stuck.status is ActionStatus.revert_failed
    assert stuck.error == INTERRUPTED
    done = await run_rewind(svc, session.id)
    assert [s.result for s in done.steps] == [StepResult.restored]
    assert fake(svc).rows[1]["price"] == 49


async def test_a_revert_whose_outcome_cant_be_saved_isnt_left_reverting(
    svc: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 1}})
    save = svc.store.save
    dropped: list[str] = []

    async def drop_once(action: Any) -> None:
        if action.status is ActionStatus.reverted and not dropped:
            dropped.append(action.id)
            raise ConnectionError("log database went away")
        await save(action)

    monkeypatch.setattr(svc.store, "save", drop_once)
    done = await run_rewind(svc, session.id)
    assert dropped and [s.result for s in done.steps] == [StepResult.restored]
    action = await svc.store.get(dropped[0])
    assert action is not None and action.status is ActionStatus.revert_failed
    assert action.error == INTERRUPTED


async def test_held_call_runs_only_after_approval(svc: Services) -> None:
    # Hold every call to pg_update_rows for this test by pretending the policy says so.
    entry = svc.gateway.policy.entry("pg_update_rows")
    assert entry is not None
    svc.gateway.policy._entries["pg_update_rows"] = entry.model_copy(
        update={"hold": "email_outbox"}
    )
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 2}]
    result = await svc.gateway.call_tool(
        session, "pg_update_rows", {"filter": flt, "values": {"price": 1}, "api_key": "sk_live_x"}
    )
    assert result.structured_content["status"] == "held"
    assert fake(svc).rows[2]["price"] == 99
    action = await svc.store.get(result.structured_content["action_id"])
    assert action is not None and action.approval is not None
    assert action.args_redacted["api_key"] == "[redacted]"
    decision = ApprovalDecisionRequest(
        decision=ApprovalDecision.approve,
        code=action.approval.code,
        channel=ApprovalChannel.dashboard,
        decided_by="dashboard",
    )
    executed = await svc.hold_manager.on_decision(action.id, decision)
    assert executed.status is ActionStatus.executed
    assert fake(svc).rows[2]["price"] == 1
    assert await svc.store.take_held_call(action.id) is None  # arguments are gone once run


async def test_an_approved_call_a_rewind_rejected_never_runs(svc: Services) -> None:
    entry = svc.gateway.policy.entry("pg_update_rows")
    assert entry is not None
    svc.gateway.policy._entries["pg_update_rows"] = entry.model_copy(
        update={"hold": "email_outbox"}
    )
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 2}]
    result = await svc.gateway.call_tool(
        session, "pg_update_rows", {"filter": flt, "values": {"price": 1}}
    )
    action = await svc.store.get(result.structured_content["action_id"])
    assert action is not None
    # Approved, then a rewind's cancel rejects it before the approval's run gets to it.
    await svc.store.save(action.model_copy(update={"status": ActionStatus.approved}))
    await svc.store.save(action.model_copy(update={"status": ActionStatus.rejected}))
    ran = await svc.gateway.run_held(action.id)
    assert ran.status is ActionStatus.rejected
    assert fake(svc).rows[2]["price"] == 99


async def test_events_are_ordered_and_replayable(svc: Services) -> None:
    received: list[str] = []

    async def listen() -> None:
        async for envelope in svc.bus.subscribe():
            received.append(envelope.root.event.value)
            if envelope.root.event.value == "action.executed":
                return

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0.05)
    session = await svc.gateway.start_session(AGENT, SessionMode.replay)
    await svc.gateway.call_tool(
        session,
        "pg_update_rows",
        {"filter": [{"column": "id", "op": "eq", "value": 1}], "values": {"price": 2}},
    )
    await asyncio.wait_for(listener, 5)
    assert received == ["session.started", "action.created", "action.classified", "action.executed"]
    replayed = await svc.bus.replay(after_seq=1)
    assert [e.root.event.value for e in replayed] == received[1:]


async def test_rest_routes(svc: Services) -> None:
    app = create_app(SETTINGS, services=svc)
    dash = {"Authorization": "Bearer dash"}
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 3}})

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gw", headers=dash) as c:
        sessions = (await c.get("/api/sessions")).json()
        assert [s["id"] for s in sessions] == [session.id]
        actions = (await c.get(f"/api/actions?session_id={session.id}")).json()
        assert [a["tool"] for a in actions] == ["pg_update_rows"]
        assert (await c.get(f"/api/actions/{actions[0]['id']}")).status_code == 200
        missing = "act_" + "0" * 26
        assert (await c.get(f"/api/actions/{missing}")).status_code == 404
        assert (await c.get("/api/actions/nope")).status_code == 422
        scope = {"session_id": session.id}
        plan = await c.post("/api/rewind", json={"scope": scope})
        assert plan.status_code == 200 and plan.json()["status"] == "planned"
        run = await c.post("/api/rewind", json={"scope": scope, "dry_run": False})
        assert run.status_code == 202
        await svc.rewinder.wait()
        job = (await c.get(f"/api/rewind/{run.json()['id']}")).json()
        assert job["status"] == "completed" and job["requested_by"] == "dashboard"
        service = {"Authorization": "", "X-Mulligan-Service-Token": "svc"}
        labels = []
        for label in ("dashboard", "asi_one", None):
            body = {"scope": scope, "requested_by": label}
            labels.append((await c.post("/api/rewind", json=body, headers=service)).json())
        assert [j["requested_by"] for j in labels] == [
            "service:dashboard",  # only the dashboard's token reads as "dashboard"
            "service:asi_one",
            "service",
        ]
        assert (await c.post("/api/rewind", json={"scope": {}})).status_code == 422
        checks = (await c.get("/api/checksums?recompute=true")).json()
        assert set(checks) == {"postgres"}
        wrong = {"Authorization": "", "X-Mulligan-Service-Token": "svc"}
        assert (await c.post("/api/demo/reset", headers=wrong)).status_code == 401
        reset = await c.post("/api/demo/reset")
        assert reset.status_code == 200 and reset.json()["ok"] is True
        assert (await c.get("/api/sessions")).json() == []


async def test_parallel_calls_are_undone_in_the_order_they_ran(
    svc: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first call is created first but reaches the system lock second, so it runs last."""
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    gate = asyncio.Event()
    bump = svc.gateway._bump_session
    calls = 0

    async def slow_first_bump(session_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            await gate.wait()
        await bump(session_id)

    monkeypatch.setattr(svc.gateway, "_bump_session", slow_first_bump)
    first = asyncio.create_task(
        svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 10}})
    )
    await asyncio.sleep(0.05)
    await svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 20}})
    gate.set()
    await first
    assert fake(svc).rows[1]["price"] == 10  # the call created first ran last

    done = await run_rewind(svc, session.id)
    assert [s.result for s in done.steps] == [StepResult.restored, StepResult.restored]
    assert fake(svc).rows[1]["price"] == 49
    assert done.verification["postgres"].ok is True


async def test_a_call_still_running_is_waited_for_and_undone(
    svc: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    flt = [{"column": "id", "op": "eq", "value": 1}]
    adapter = fake(svc)
    gate = asyncio.Event()
    execute = adapter.execute

    async def slow_execute(call: Any, before: Any) -> Any:
        await gate.wait()
        return await execute(call, before)

    monkeypatch.setattr(adapter, "execute", slow_execute)
    running = asyncio.create_task(
        svc.gateway.call_tool(session, "pg_update_rows", {"filter": flt, "values": {"price": 0}})
    )
    await asyncio.sleep(0.05)
    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert [(s.result, s.detail) for s in plan.steps] == [
        (StepResult.pending, "Still running. The rewind waits for it.")
    ]
    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await asyncio.sleep(0.05)
    gate.set()
    await running
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None and done.status is RewindJobStatus.completed
    assert [s.result for s in done.steps] == [StepResult.restored]
    assert adapter.rows[1]["price"] == 49
    action = await svc.store.get(done.steps[0].action_id)
    assert action is not None and action.status is ActionStatus.reverted


async def test_event_seq_keeps_counting_after_a_demo_reset(svc: Services) -> None:
    first = await svc.gateway.start_session(AGENT, SessionMode.live)
    before = await svc.bus.replay(0)
    await svc.store.clear()
    assert await svc.bus.replay(0) == []
    await svc.gateway.start_session(AGENT, SessionMode.live)
    after = await svc.bus.replay(0)
    assert first.id and after[0].root.seq > before[-1].root.seq
