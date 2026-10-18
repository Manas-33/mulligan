"""A rewind step gives up on a call that never settles instead of hanging the rewind."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from mulligan.models import Action, StepResult
from mulligan.orchestrator import rewind
from mulligan.orchestrator.rewind import Rewinder

OPENAPI = Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml"


class StuckHolds:
    """An approved call whose running flag is never cleared."""

    def __init__(self) -> None:
        self.cancelled = False

    async def settled(self, action_id: str) -> None:
        await asyncio.Event().wait()

    async def cancel(self, action: Action) -> None:
        self.cancelled = True


async def test_a_call_that_never_settles_fails_its_step(monkeypatch: pytest.MonkeyPatch) -> None:
    example = yaml.safe_load(OPENAPI.read_text())["components"]["examples"]["ActionEmailHeld"]
    action = Action.model_validate({**example["value"], "status": "approved"})

    async def get(action_id: str) -> Action:
        return action

    async def settled(action_id: str) -> None:
        return None

    holds = StuckHolds()
    gateway = SimpleNamespace(
        store=SimpleNamespace(get=get),
        bus=None,
        hold_manager=holds,
        by_system={},
        settled=settled,
    )
    monkeypatch.setattr(rewind, "SETTLE_TIMEOUT_S", 0.05)
    step, after = await asyncio.wait_for(
        Rewinder(gateway)._revert_one(action),  # type: ignore[arg-type]
        timeout=2,
    )
    assert step.result is StepResult.failed
    assert step.detail is not None and "Still running after 0.05 s" in step.detail
    assert after is action and not holds.cancelled
