"""ASI:One shows only the last chat message per turn, so the Guardian sends exactly one."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

from tests.test_guardian import DELETE, FakeAPI


class FakeContext:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

        class _Log:
            def info(self, *_: Any) -> None: ...
            def debug(self, *_: Any) -> None: ...

        self.logger = _Log()

    async def send(self, to: str, message: Any) -> None:
        self.sent.append((to, message))

    def chats(self) -> list[ChatMessage]:
        return [m for _, m in self.sent if isinstance(m, ChatMessage)]


@pytest.fixture
def guardian(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("MULLIGAN_DOTENV", "/nonexistent")
    monkeypatch.setenv("GUARDIAN_AGENT_SEED", "unit test guardian seed phrase 0001")
    monkeypatch.setenv("WORKER_AGENT_SEED", "unit test worker seed phrase 0002")
    # uagents' Agent() looks up the current event loop when the module is imported.
    asyncio.set_event_loop(asyncio.new_event_loop())
    module = importlib.import_module("mulligan_fetch.guardian")
    from mulligan_fetch.guardian_logic import GuardianLogic

    monkeypatch.setattr(module, "logic", GuardianLogic(FakeAPI()))  # type: ignore[arg-type]
    module.pending.clear()
    return module


def chat(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(UTC), msg_id=uuid4(), content=[TextContent(type="text", text=text)]
    )


@pytest.mark.parametrize("text", ["help", "status", "rewind", "approve 4821", "reject 4821"])
async def test_every_command_gets_exactly_one_chat_message(guardian: Any, text: str) -> None:
    guardian.logic.last_session["user1"] = DELETE["session_id"]
    ctx = FakeContext()
    await guardian.on_chat(ctx, "user1", chat(text))
    assert isinstance(ctx.sent[0][1], ChatAcknowledgement)
    assert len(ctx.chats()) == 1


async def test_a_task_gets_exactly_one_chat_message_the_report(guardian: Any) -> None:
    ctx = FakeContext()
    await guardian.on_chat(ctx, "user1", chat("do the quarter-end cleanup"))
    assert ctx.chats() == []  # no "on it" message
    [(to, request)] = [(t, m) for t, m in ctx.sent if not isinstance(m, ChatAcknowledgement)]
    assert to == guardian.worker_address
    result = guardian.TaskResult(
        request_id=request.request_id,
        task=request.task,
        steps=[],
        action_ids=[DELETE["id"]],
    )
    after = FakeContext()
    await guardian.on_result(after, guardian.worker_address, result)
    [report] = after.chats()
    kinds = [type(c).__name__ for c in report.content]
    assert kinds == ["TextContent", "MetadataContent"]
    payload = json.loads(report.content[1].metadata["card_payload"])
    assert payload["approve_cta"]["label"] == "Rewind"
