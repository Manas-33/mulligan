"""Stand-ins the core uses until Stream B's `classify/` and `holds/` provide the real ones."""

from __future__ import annotations

import secrets
from datetime import timedelta

from mulligan.core import utcnow
from mulligan.interfaces import (
    Classifier,
    HoldDeps,
    HoldManager,
    HoldNotFound,
    NotHeld,
    UpstreamToolSchema,
    WrongCode,
)
from mulligan.models import (
    Action,
    ActionStatus,
    Approval,
    ApprovalDecision,
    ApprovalDecisionRequest,
    EventName,
    Hold,
    HoldKind,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    RewindStep,
    StepResult,
)

HOLD_TTL = timedelta(minutes=15)


class HoldEverythingClassifier(Classifier):
    def classify_unknown(self, tool_schema: UpstreamToolSchema) -> Reversibility:
        return Reversibility(
            label=ReversibilityLabel.irreversible,
            source=ReversibilitySource.rules,
            reason="no rules gate installed yet, so every upstream call is held",
        )


class BasicHoldManager(HoldManager):
    """Approve or reject from the dashboard only. No texts, no payments, no expiry."""

    def __init__(self, deps: HoldDeps) -> None:
        self.deps = deps

    async def _codes_in_use(self) -> set[str]:
        held = await self.deps.store.list_by_status(ActionStatus.held.value)
        return {a.approval.code for a in held if a.approval}

    async def hold(self, action: Action, hold_kind: HoldKind | None) -> Action:
        used = await self._codes_in_use()
        code = next(
            c for c in (f"{secrets.randbelow(10_000):04d}" for _ in range(100)) if c not in used
        )
        now = action.created_at
        action = action.model_copy(
            update={
                "status": ActionStatus.held,
                "approval": Approval(
                    code=code,
                    channel=None,
                    requested_at=now,
                    expires_at=now + HOLD_TTL,
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
        return action

    async def on_decision(self, action_id: str, decision: ApprovalDecisionRequest) -> Action:
        action = await self.deps.store.get(action_id)
        if action is None:
            raise HoldNotFound(action_id)
        if action.status is not ActionStatus.held or action.approval is None:
            raise NotHeld(f"action is {action.status.value}, not held")
        if decision.code != action.approval.code:
            raise WrongCode("code does not match")
        approval = action.approval.model_copy(
            update={
                "channel": decision.channel,
                "decided_at": utcnow(),
                "decided_by": decision.decided_by,
                "decision": decision.decision,
            }
        )
        if decision.decision is ApprovalDecision.reject:
            action = action.model_copy(
                update={"status": ActionStatus.rejected, "approval": approval}
            )
            await self.deps.store.save(action)
            await self.deps.bus.publish(EventName.action_rejected, action)
            return action
        action = action.model_copy(update={"status": ActionStatus.approved, "approval": approval})
        await self.deps.store.save(action)
        await self.deps.bus.publish(EventName.action_approved, action)
        return await self.deps.runner.run_held(action_id)

    async def release_due(self) -> list[Action]:
        return []

    async def cancel(self, action: Action) -> RewindStep:
        if action.status in (ActionStatus.held, ActionStatus.approved):
            action = action.model_copy(update={"status": ActionStatus.rejected})
            await self.deps.store.save(action)
            await self.deps.bus.publish(EventName.action_rejected, action)
            detail = "Rejected while held. Nothing ran."
        else:
            detail = f"Action is {action.status.value}; nothing to cancel."
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=StepResult.skipped,
            kept_fields=[],
            changes=[],
            changes_truncated=False,
            detail=detail,
        )
