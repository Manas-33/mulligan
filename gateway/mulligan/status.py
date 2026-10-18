"""`mulligan_status`: lets an agent see its own session and what's waiting for approval."""

from __future__ import annotations

from typing import ClassVar

from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult
from mulligan.log.store import LogStore
from mulligan.models import Action, ActionStatus, Inverse, RewindStep, StepResult, System


class StatusAdapter(Adapter):
    system: ClassVar[System] = System.mulligan
    tools: ClassVar[frozenset[str]] = frozenset({"mulligan_status"})

    def __init__(self, store: LogStore) -> None:
        self.store = store

    async def capture_before(self, call: ToolCall) -> Capture:
        return Capture()

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        actions = await self.store.list_actions(session_id=call.session_id, limit=None)
        held = [a for a in actions if a.status is ActionStatus.held]
        recent = [a for a in actions if a.id != call.action_id][-10:]
        return ToolResult(
            structured_content={
                "session_id": call.session_id,
                "agent": call.agent.id,
                "action_count": len(actions),
                "held": [{"action_id": a.id, "tool": a.tool, "summary": a.summary} for a in held],
                "recent": [
                    {
                        "action_id": a.id,
                        "tool": a.tool,
                        "status": a.status.value,
                        "summary": a.summary,
                    }
                    for a in recent
                ],
            },
            summary=f"{len(actions)} actions in this session, {len(held)} held",
        )

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        return Capture()

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        return None

    async def plan_revert(self, action: Action) -> RewindStep:
        return await self.revert(action)

    async def revert(self, action: Action) -> RewindStep:
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=StepResult.skipped,
            kept_fields=[],
            changes=[],
            changes_truncated=False,
            detail="Read only.",
        )

    async def checksum(self) -> str | None:
        return None
