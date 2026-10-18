"""Rewind: undo a set of actions newest first, then verify each system's checksum."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import sentry_sdk

from mulligan.core import Gateway, new_id, utcnow
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    Action,
    ActionStatus,
    ChecksumVerifiedData,
    EventName,
    ReversibilityLabel,
    RewindJob,
    RewindJobStatus,
    RewindRequest,
    RewindStep,
    RewindStepData,
    SessionStatus,
    StepResult,
    System,
    SystemVerification,
)

# Statuses whose action changed something (or may still) and so belongs in a rewind.
_UNDOABLE = {ActionStatus.executed, ActionStatus.held, ActionStatus.approved}
_AFTER_STEP = {
    StepResult.restored: ActionStatus.reverted,
    # A refund or a correction email offsets the effect; the step result says which it was.
    StepResult.compensated: ActionStatus.reverted,
    StepResult.kept_human_edit: ActionStatus.conflict,
    StepResult.failed: ActionStatus.revert_failed,
}


class RewindBusy(RuntimeError):
    pass


class EmptyScope(ValueError):
    """A scope with no fields would select every action in the log."""


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


class Rewinder:
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self.store: LogStore = gateway.store
        self.bus: EventBus = gateway.bus
        self.running: str | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.on_finished: list[Callable[[RewindJob], object]] = []

    async def select(self, request: RewindRequest) -> list[Action]:
        """Actions in scope that changed something, newest first."""
        scope = request.scope
        actions = await self.store.list_actions(
            session_id=scope.session_id,
            since=scope.since,
            ids=scope.action_ids,
            newest_first=True,
            limit=None,
        )
        return [
            a
            for a in actions
            if a.status in _UNDOABLE and a.reversibility.label is not ReversibilityLabel.read_only
        ]

    async def plan_step(self, action: Action) -> RewindStep:
        if action.status in (ActionStatus.held, ActionStatus.approved):
            return _step(action, StepResult.pending, "Still held. The rewind rejects it.")
        if action.system is System.upstream:
            return _step(action, StepResult.pending, "Upstream calls can't be undone; skipped.")
        adapter = self.gateway.by_system.get(action.system)
        if adapter is None:
            return _step(action, StepResult.pending, f"No adapter for {action.system.value}.")
        return await adapter.plan_revert(action)

    async def expected(self, actions: list[Action]) -> dict[str, SystemVerification]:
        """Per system, the checksum just before the oldest action in scope ran."""
        pre = await self.store.pre_checksums([a.id for a in actions])
        expected: dict[str, SystemVerification] = {}
        for action in actions:  # newest first, so the oldest wins
            if action.id in pre:
                expected[action.system.value] = SystemVerification(
                    expected=pre[action.id], actual=None, ok=None
                )
        return expected

    async def create(self, request: RewindRequest) -> RewindJob:
        scope = request.scope
        if not (scope.session_id or scope.since or scope.action_ids):
            raise EmptyScope("scope needs session_id, since or action_ids")
        if not request.dry_run and self.running:
            raise RewindBusy(f"rewind {self.running} is running")
        actions = await self.select(request)
        job = RewindJob(
            id=new_id("rwd"),
            scope=request.scope,
            dry_run=bool(request.dry_run),
            status=RewindJobStatus.planned,
            steps=[await self.plan_step(a) for a in actions],
            verification=await self.expected(actions),
            requested_by=request.requested_by,
            error=None,
            created_at=utcnow(),
            started_at=None,
            completed_at=None,
        )
        if job.dry_run:
            await self.store.save_job(job)
            await self.bus.publish(EventName.rewind_planned, job)
            return job
        if self.running:
            raise RewindBusy(f"rewind {self.running} is running")
        self.running = job.id
        job = job.model_copy(update={"status": RewindJobStatus.running, "started_at": utcnow()})
        await self.store.save_job(job)
        await self.bus.publish(EventName.rewind_started, job)
        task = asyncio.create_task(self._run(job, actions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def wait(self) -> None:
        """Wait for running rewinds (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _run(self, job: RewindJob, actions: list[Action]) -> None:
        try:
            with sentry_sdk.start_span(op="mulligan.rewind", name=job.id):
                job = await self._run_steps(job, actions)
                job = await self._verify(job)
            failed = any(s.result is StepResult.failed for s in job.steps)
            mismatch = any(v.ok is False for v in job.verification.values())
            status = RewindJobStatus.partial if failed or mismatch else RewindJobStatus.completed
            job = job.model_copy(update={"status": status, "completed_at": utcnow()})
            await self.store.save_job(job)
            await self._mark_session(job)
            await self.bus.publish(EventName.rewind_completed, job)
        except Exception as exc:  # noqa: BLE001 (reported on the job)
            sentry_sdk.capture_exception(exc)
            job = job.model_copy(
                update={
                    "status": RewindJobStatus.failed,
                    "error": str(exc)[:500] or type(exc).__name__,
                    "completed_at": utcnow(),
                }
            )
            await self.store.save_job(job)
            await self.bus.publish(EventName.rewind_failed, job)
        finally:
            self.running = None
            for callback in self.on_finished:
                callback(job)

    async def _run_steps(self, job: RewindJob, actions: list[Action]) -> RewindJob:
        steps = list(job.steps)
        for index, action in enumerate(actions):
            with sentry_sdk.start_span(op="mulligan.rewind.step", name=action.tool) as span:
                span.set_data("mulligan.action_id", action.id)
                step, action = await self._revert_one(action)
            steps[index] = step
            job = job.model_copy(update={"steps": list(steps)})
            await self.store.save_job(job)
            await self.bus.publish(
                EventName.rewind_step,
                RewindStepData(rewind_job_id=job.id, step=step, action=action),
            )
        return job

    async def _revert_one(self, action: Action) -> tuple[RewindStep, Action]:
        current = await self.store.get(action.id) or action
        if current.status in (ActionStatus.held, ActionStatus.approved):
            hold_manager = self.gateway.hold_manager
            if hold_manager is None:
                return _step(current, StepResult.failed, "No hold manager."), current
            step = await hold_manager.cancel(current)
            await self.store.drop_held_call(current.id)
            return step, await self.store.get(current.id) or current
        if current.status is not ActionStatus.executed:
            return _step(current, StepResult.skipped, f"Action is {current.status.value}."), current
        if current.system is System.upstream:
            return _step(current, StepResult.skipped, "Upstream calls can't be undone."), current
        adapter = self.gateway.by_system.get(current.system)
        if adapter is None:
            return _step(
                current, StepResult.failed, f"No adapter for {current.system.value}."
            ), current
        async with self.gateway.lock_for(adapter.system):
            reverting = current.model_copy(update={"status": ActionStatus.reverting})
            await self.store.save(reverting)
            try:
                step = await adapter.revert(reverting)
            except Exception as exc:  # noqa: BLE001 (a failed step doesn't stop the rewind)
                sentry_sdk.capture_exception(exc)
                step = _step(current, StepResult.failed, str(exc)[:500] or type(exc).__name__)
        status = _AFTER_STEP.get(step.result, ActionStatus.executed)
        update: dict[str, object] = {"status": status}
        if status in (ActionStatus.reverted, ActionStatus.conflict):
            update["reverted_at"] = utcnow()
        if status is ActionStatus.revert_failed:
            update["error"] = step.detail
        done = current.model_copy(update=update)
        await self.store.save(done)
        return step, done

    async def _verify(self, job: RewindJob) -> RewindJob:
        verification = dict(job.verification)
        for system, check in job.verification.items():
            adapter = self.gateway.by_system.get(System(system))
            expected, actual = check.expected, None
            if adapter is not None:
                steps = [s for s in job.steps if s.system.value == system]
                expected = await adapter.expected_checksum(check.expected, steps)
                actual = await adapter.checksum()
            ok = actual is not None and actual == expected
            verification[system] = SystemVerification(expected=expected, actual=actual, ok=ok)
            await self.bus.publish(
                EventName.checksum_verified,
                ChecksumVerifiedData(
                    system=System(system),
                    expected=expected,
                    actual=actual or "",
                    ok=ok,
                    rewind_job_id=job.id,
                ),
            )
        return job.model_copy(update={"verification": verification})

    async def _mark_session(self, job: RewindJob) -> None:
        if not job.scope.session_id:
            return
        session = await self.store.get_session(job.scope.session_id)
        if session is not None:
            await self.store.save_session(
                session.model_copy(update={"status": SessionStatus.rewound})
            )
