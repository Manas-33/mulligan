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

# Statuses whose action changed something (or may still) and so belongs in a rewind. A step that
# failed is tried again: adapters compare current values with the agent's before writing.
_UNDOABLE = {
    ActionStatus.executed,
    ActionStatus.held,
    ActionStatus.approved,
    ActionStatus.revert_failed,
}
_AFTER_STEP = {
    StepResult.restored: ActionStatus.reverted,
    # A refund or a correction email offsets the effect; the step result says which it was.
    StepResult.compensated: ActionStatus.reverted,
    StepResult.kept_human_edit: ActionStatus.conflict,
    StepResult.failed: ActionStatus.revert_failed,
}


# Sentry span status for each finished job.
SPAN_STATUS = {
    RewindJobStatus.completed: "ok",
    RewindJobStatus.partial: "internal_error",
    RewindJobStatus.failed: "internal_error",
    RewindJobStatus.running: "unknown",
    RewindJobStatus.planned: "ok",
}


MAY_HAVE_LANDED = "The call failed, but its change may have landed. "
INTERRUPTED = "The revert was interrupted before its outcome was saved; rewind again to check it."


def may_have_landed(action: Action) -> bool:
    """A call that failed but kept an inverse (`WriteUncertain`): a rewind checks for its change.
    It never ran to completion, so it has no executed_at whatever its status since."""
    return action.executed_at is None and action.inverse is not None


def undoable(action: Action) -> bool:
    """Whether a rewind picks this action up."""
    if action.reversibility.label is ReversibilityLabel.read_only:
        return False
    if action.status is ActionStatus.failed:
        return may_have_landed(action)
    return action.status in _UNDOABLE


class RewindBusy(RuntimeError):
    pass


class EmptyScope(ValueError):
    """A scope with no fields would select every action in the log."""


def _flag(action: Action, step: RewindStep) -> RewindStep:
    """A step for a call that may have landed says so."""
    if not may_have_landed(action):
        return step
    return step.model_copy(update={"detail": MAY_HAVE_LANDED + (step.detail or "")})


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
        return [a for a in actions if undoable(a)]

    async def recover(self) -> list[Action]:
        """Mark actions left ``reverting`` (the gateway stopped, or the log was unreachable, in
        the middle of a revert) ``revert_failed``, so the next rewind tries them again. Only
        while no rewind runs: until then, a ``reverting`` action may be in progress."""
        recovered: list[Action] = []
        for action in await self.store.list_by_status(ActionStatus.reverting):
            if self.running:
                break
            action = action.model_copy(
                update={"status": ActionStatus.revert_failed, "error": INTERRUPTED}
            )
            await self.store.save(action)
            recovered.append(action)
        return recovered

    async def plan_step(self, action: Action) -> RewindStep:
        if action.status in (ActionStatus.held, ActionStatus.approved):
            return _step(action, StepResult.pending, "Still held. The rewind rejects it.")
        if action.system is System.upstream:
            return _step(action, StepResult.pending, "Upstream calls can't be undone; skipped.")
        adapter = self.gateway.by_system.get(action.system)
        if adapter is None:
            return _step(action, StepResult.pending, f"No adapter for {action.system.value}.")
        return _flag(action, await adapter.plan_revert(action))

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
        if not self.running:
            await self.recover()
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
        # Its own transaction: this task outlives the POST /api/rewind request that started it,
        # so a span here would have no live parent and Sentry would drop it.
        with sentry_sdk.start_transaction(op="mulligan.rewind", name=job.id) as transaction:
            transaction.set_tag("mulligan.rewind_job_id", job.id)
            transaction.set_tag("mulligan.session_id", job.scope.session_id or "")
            transaction.set_data(
                "mulligan.scope", job.scope.model_dump(mode="json", exclude_none=True)
            )
            transaction.set_data("mulligan.actions", len(actions))
            job = await self._run_job(job, actions)
            transaction.set_tag("mulligan.rewind_status", job.status.value)
            transaction.set_status(SPAN_STATUS[job.status])

    async def _run_job(self, job: RewindJob, actions: list[Action]) -> RewindJob:
        try:
            job = await self._run_steps(job, actions)
            job = await self._verify(job, actions)
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
        return job

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
        if not undoable(current):
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
        step = _flag(current, step)
        # Nothing undone: a call that failed stays failed, one that ran stays executed.
        unchanged = ActionStatus.failed if may_have_landed(current) else ActionStatus.executed
        status = _AFTER_STEP.get(step.result, unchanged)
        update: dict[str, object] = {"status": status}
        if status in (ActionStatus.reverted, ActionStatus.conflict):
            update["reverted_at"] = utcnow()
            if current.status is ActionStatus.revert_failed and not may_have_landed(current):
                update["error"] = None  # the earlier failed attempt's reason
        if status is ActionStatus.revert_failed:
            update["error"] = step.detail
        done = current.model_copy(update=update)
        try:
            await self.store.save(done)
        except Exception as exc:  # noqa: BLE001 (never leave it reverting)
            sentry_sdk.capture_exception(exc)
            done = current.model_copy(
                update={"status": ActionStatus.revert_failed, "error": INTERRUPTED}
            )
            await self.store.save(done)
        return step, done

    async def _verify(self, job: RewindJob, actions: list[Action]) -> RewindJob:
        verification = dict(job.verification)
        for system, check in job.verification.items():
            adapter = self.gateway.by_system.get(System(system))
            expected, actual = check.expected, None
            if adapter is not None:
                steps = [s for s in job.steps if s.system.value == system]
                mine = [a for a in actions if a.system.value == system]
                scoped = await adapter.verify_scope(mine, steps)
                if scoped is not None:
                    expected, actual = scoped
                else:
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
