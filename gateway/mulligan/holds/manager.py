"""The approval state machine for held calls: held -> approved | rejected | expired.

Replaces the core's dashboard-only stub. On hold it texts the approver through the Photon service
(in the background, so the agent's call isn't delayed by iMessage), and `release_due` expires
holds nobody decided. Capturing payments past `hold.release_at` arrives with the payments adapter
in Phase 3.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timedelta

from mulligan.core import utcnow
from mulligan.holds.notify import NotifyOutcome, PhotonNotifier, holds_env
from mulligan.holds.payments import CANCEL, REFUND, PaymentsAdapter, release_time, undo_window
from mulligan.interfaces import HoldDeps, HoldManager, HoldNotFound, NotHeld, WrongCode
from mulligan.models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalDecision,
    ApprovalDecisionRequest,
    EventName,
    Hold,
    HoldKind,
    RewindStep,
    StepResult,
)

log = logging.getLogger("mulligan.holds")

DEFAULT_TTL = timedelta(minutes=15)
MAX_WRONG_CODES = 5
"""Wrong codes for one hold before it is rejected: 4 digits are otherwise easy to guess."""


def hold_ttl() -> timedelta | None:
    """MULLIGAN_HOLD_TTL_MINUTES (default 15). 0 means holds never expire."""
    raw = holds_env().get("MULLIGAN_HOLD_TTL_MINUTES", "").strip()
    minutes = float(raw) if raw else DEFAULT_TTL.total_seconds() / 60
    return timedelta(minutes=minutes) if minutes > 0 else None


class CodesExhausted(RuntimeError):
    """Every 4-digit code is taken by a held action."""


class PhotonHoldManager(HoldManager):
    def __init__(
        self,
        deps: HoldDeps,
        notifier: PhotonNotifier,
        ttl: timedelta | None = DEFAULT_TTL,
        payments: PaymentsAdapter | None = None,
        payment_window: timedelta | None = None,
    ) -> None:
        self.payments = payments
        self.payment_window = payment_window or undo_window()
        self.deps = deps
        self.notifier = notifier
        self.ttl = ttl
        self._lock = asyncio.Lock()  # one hold/decision at a time: codes stay unique
        self._tasks: set[asyncio.Task[NotifyOutcome]] = set()
        # Approved calls whose run is in progress: a rewind's cancel waits for these.
        self._running: dict[str, asyncio.Event] = {}
        self._wrong: dict[str, int] = {}

    # --- hold -----------------------------------------------------------------------------------

    async def _new_code(self) -> str:
        held = await self.deps.store.list_by_status(ActionStatus.held.value)
        used = {a.approval.code for a in held if a.approval}
        for _ in range(200):
            code = f"{secrets.randbelow(10_000):04d}"
            if code not in used:
                return code
        free = sorted({f"{n:04d}" for n in range(10_000)} - used)
        if not free:
            raise CodesExhausted("all 10,000 approval codes are held")
        return free[0]

    async def hold(self, action: Action, hold_kind: HoldKind | None) -> Action:
        async with self._lock:
            now = utcnow()
            action = action.model_copy(
                update={
                    "status": ActionStatus.held,
                    "approval": Approval(
                        code=await self._new_code(),
                        channel=None,
                        requested_at=now,
                        expires_at=now + self.ttl if self.ttl else None,
                        decided_at=None,
                        decided_by=None,
                        decision=None,
                    ),
                    "hold": Hold(
                        kind=hold_kind,
                        release_at=None,
                        authorization_expires_at=None,
                        external_id=None,
                    )
                    if hold_kind
                    else None,
                }
            )
            await self.deps.store.save(action)
        await self.deps.bus.publish(EventName.action_held, action)
        task = asyncio.create_task(self.notifier.notify_approval(action))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return action

    async def wait_for_notifications(self) -> None:
        """Let pending texts finish (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- decide ---------------------------------------------------------------------------------

    async def on_decision(self, action_id: str, decision: ApprovalDecisionRequest) -> Action:
        async with self._lock:
            action = await self.deps.store.get(action_id)
            if action is None:
                raise HoldNotFound(action_id)
            if action.status is not ActionStatus.held or action.approval is None:
                raise NotHeld(f"action is {action.status.value}, not held")
            expires_at = action.approval.expires_at
            if expires_at is not None and expires_at <= utcnow():
                raise NotHeld("the hold has expired")
            if not secrets.compare_digest(decision.code, action.approval.code):
                wrong = self._wrong.get(action_id, 0) + 1
                self._wrong[action_id] = wrong
                if wrong < MAX_WRONG_CODES:
                    raise WrongCode("code does not match")
                rejected = action.model_copy(update={"status": ActionStatus.rejected})
                await self.deps.store.save(rejected)
                await self.deps.store.drop_held_call(action_id)
                await self.deps.bus.publish(EventName.action_rejected, rejected)
                raise WrongCode(f"{wrong} wrong codes; the hold is rejected")
            self._wrong.pop(action_id, None)
            approve = decision.decision is ApprovalDecision.approve
            action = action.model_copy(
                update={
                    "status": ActionStatus.approved if approve else ActionStatus.rejected,
                    "approval": action.approval.model_copy(
                        update={
                            "channel": decision.channel,
                            "decided_at": utcnow(),
                            "decided_by": decision.decided_by,
                            "decision": decision.decision,
                        }
                    ),
                }
            )
            await self.deps.store.save(action)
            done = asyncio.Event()
            if approve:
                self._running[action_id] = done
        await self.deps.bus.publish(
            EventName.action_approved if approve else EventName.action_rejected, action
        )
        if not approve:
            await self.deps.store.drop_held_call(action_id)
            return action
        try:
            try:
                ran = await self.deps.runner.run_held(action_id)
            except Exception as exc:  # noqa: BLE001 (the approval is recorded; never leave it "approved")
                log.exception("running approved action %s failed", action_id)
                current = await self.deps.store.get(action_id) or action
                if current.status not in (ActionStatus.approved, ActionStatus.executing):
                    return current  # it got further (or a rewind rejected it): keep that
                failed = current.model_copy(
                    update={"status": ActionStatus.failed, "error": f"{type(exc).__name__}: {exc}"}
                )
                await self.deps.store.save(failed)
                await self.deps.bus.publish(EventName.action_failed, failed)
                return failed
            try:
                return await self._record_payment_hold(ran)
            except Exception:  # noqa: BLE001 (it ran; _capture_due fills in release_at)
                log.exception("recording the payment hold of %s failed", action_id)
                return ran
        finally:
            self._running.pop(action_id, None)
            done.set()

    async def _record_payment_hold(self, action: Action) -> Action:
        """After an approved payment is authorized, put the PaymentIntent and the undo window on
        `action.hold` (the core stores only the adapter's inverse)."""
        # Re-read: a rewind may have undone it since it ran.
        action = await self.deps.store.get(action.id) or action
        if (
            action.status is not ActionStatus.executed
            or action.hold is None
            or action.inverse is None
            or action.inverse.kind != CANCEL
        ):
            return action
        payload = action.inverse.payload
        capture_before = datetime.fromisoformat(payload["capture_before"])
        action = action.model_copy(
            update={
                "hold": action.hold.model_copy(
                    update={
                        "external_id": payload["payment_intent"],
                        "authorization_expires_at": capture_before,
                        "release_at": release_time(utcnow(), capture_before, self.payment_window),
                    }
                )
            }
        )
        await self.deps.store.save(action)
        # Same event, full object: the dashboard replaces its copy and shows the capture time.
        await self.deps.bus.publish(EventName.action_executed, action)
        return action

    async def _capture_due(self, now: datetime) -> list[Action]:
        """Capture authorized payments whose undo window has ended."""
        if self.payments is None:
            return []
        captured: list[Action] = []
        for action in await self.deps.store.list_by_status(ActionStatus.executed.value):
            hold, inverse = action.hold, action.inverse
            if (
                action.system is not self.payments.system
                or hold is None
                or inverse is None
                or inverse.kind != CANCEL
                or inverse.payload.get("capture_status") == "canceled"
            ):
                continue
            release_at = hold.release_at
            if release_at is None and action.executed_at is not None:
                # The gateway stopped before the hold was recorded: the window ran from then.
                capture_before = datetime.fromisoformat(inverse.payload["capture_before"])
                release_at = release_time(action.executed_at, capture_before, self.payment_window)
            if release_at is None or release_at > now:
                continue
            try:
                status = await self.payments.capture(action)
            except Exception:  # noqa: BLE001 (retried next tick; Sentry gets it)
                log.exception("capturing %s failed", action.id)
                continue
            # Re-read: a rewind may have cancelled or refunded it meanwhile; don't undo that.
            current = await self.deps.store.get(action.id) or action
            if current.status is not ActionStatus.executed or current.inverse is None:
                continue
            if status == "canceled":
                log.error("%s: the authorization was cancelled before capture", action.id)
                payload = {**current.inverse.payload, "capture_status": "canceled"}
                inverse_update = current.inverse.model_copy(update={"payload": payload})
                await self.deps.store.save(current.model_copy(update={"inverse": inverse_update}))
                continue
            if status != "succeeded":
                log.error("capture of %s returned %s", action.id, status)
                continue
            current = current.model_copy(
                update={
                    "inverse": current.inverse.model_copy(
                        update={
                            "kind": REFUND,
                            "payload": {**current.inverse.payload, "captured_at": now.isoformat()},
                        }
                    )
                }
            )
            await self.deps.store.save(current)
            captured.append(current)
        return captured

    # --- expire ---------------------------------------------------------------------------------

    async def release_due(self) -> list[Action]:
        now = utcnow()
        changed: list[Action] = []
        async with self._lock:
            for action in await self.deps.store.list_by_status(ActionStatus.held.value):
                expires_at = action.approval.expires_at if action.approval else None
                if expires_at is None or expires_at > now:
                    continue
                action = action.model_copy(update={"status": ActionStatus.expired})
                await self.deps.store.save(action)
                await self.deps.store.drop_held_call(action.id)
                changed.append(action)
            captured = await self._capture_due(now)
        for action in changed:
            await self.deps.bus.publish(EventName.hold_expired, action)
        for action in captured:
            await self.deps.bus.publish(EventName.hold_released, action)
        return changed + captured

    # --- rewind before it ran -------------------------------------------------------------------

    async def cancel(self, action: Action) -> RewindStep:
        running = self._running.get(action.id)
        if running is not None:
            # Approved and running now: too late to stop. Wait for it, so the rewind sees it
            # executed and undoes it (refund, correction email) like any other call.
            await running.wait()
        async with self._lock:
            current = await self.deps.store.get(action.id) or action
            # Approved but not running (the gateway stopped mid-approval): the core's run_held
            # won't run it once it's rejected.
            if current.status in (ActionStatus.held, ActionStatus.approved):
                current = current.model_copy(update={"status": ActionStatus.rejected})
                await self.deps.store.save(current)
                ran = False
            else:
                ran = True
        if not ran:
            await self.deps.bus.publish(EventName.action_rejected, current)
        return RewindStep(
            action_id=current.id,
            system=current.system,
            tool=current.tool,
            result=StepResult.skipped,
            kept_fields=[],
            changes=[],
            changes_truncated=False,
            detail=(
                f"Action is {current.status.value}; nothing to cancel."
                if ran
                else "Rejected while held. Nothing ran."
            ),
        )
