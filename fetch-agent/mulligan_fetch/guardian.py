"""The Guardian agent: talks to people in ASI:One (Agent Chat Protocol, mailbox), hands tasks to
the worker, reports what Mulligan recorded and held, and rewinds on request.

  uv run python -m mulligan_fetch.guardian
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.identity import Identity

from mulligan_fetch.config import Config
from mulligan_fetch.guardian_logic import GuardianLogic, Reply, parse
from mulligan_fetch.messages import TaskRequest, TaskResult
from mulligan_fetch.mulligan_api import MulliganAPI

HERE = Path(__file__).resolve().parents[1]
config = Config.from_env()

guardian = Agent(
    name="mulligan-guardian",
    seed=config.guardian_seed,
    port=config.guardian_port,
    mailbox=True,
    publish_agent_details=True,
    readme_path=str(HERE / "README.md"),
    description=(
        "Runs agent tasks through the Mulligan gateway, which records every change and holds "
        "irreversible ones, and rewinds the whole session when you say so."
    ),
)
worker_address = Identity.from_seed(config.worker_seed, 0).address
logic = GuardianLogic(MulliganAPI(config.api_url, config.service_token))
pending: dict[str, str] = {}  # request id -> chat sender
# ASI:One can deliver the same chat message again while a slow reply (a rewind) is running;
# acknowledge repeats but act once.
seen_messages: deque[str] = deque(maxlen=500)
chat = Protocol(spec=chat_protocol_spec)


def to_message(reply: Reply) -> ChatMessage:
    """The turn's one chat message: always non-empty text, with the card and the end-session
    marker riding on it (ASI:One shows a message without text as an empty bubble)."""
    text = reply.text.strip() or "Done."
    content: list[TextContent | MetadataContent | EndSessionContent] = [
        TextContent(type="text", text=text)
    ]
    if reply.card:
        content.append(MetadataContent(type="metadata", metadata=reply.card))
    if reply.end_session:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(UTC), msg_id=uuid4(), content=content)


@chat.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(
        sender, ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=msg.msg_id)
    )
    if str(msg.msg_id) in seen_messages:
        ctx.logger.info(f"repeat of {msg.msg_id} from {sender}; already handled")
        return
    seen_messages.append(str(msg.msg_id))
    text = "".join(c.text for c in msg.content if isinstance(c, TextContent))
    if not text.strip():
        return  # e.g. a bare StartSessionContent: nothing to answer
    command = parse(text)
    ctx.logger.info(f"chat from {sender}: {command.kind}")
    if command.kind == "task":
        request_id = uuid4().hex
        pending[request_id] = sender
        # No "on it" message: ASI:One shows only the last message per turn, so the report that
        # comes back from the worker is the one reply.
        await ctx.send(worker_address, TaskRequest(request_id=request_id, task=command.text))
        return
    await ctx.send(sender, to_message(await logic.handle(sender, command)))


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    ctx.logger.debug(f"ack from {sender} for {msg.acknowledged_msg_id}")


@guardian.on_message(model=TaskResult)
async def on_result(ctx: Context, sender: str, msg: TaskResult) -> None:
    chat_sender = pending.pop(msg.request_id, None)
    if chat_sender is None or sender != worker_address:
        return
    if msg.error:
        reply = Reply(f"The worker failed: {msg.error}", end_session=True)
    else:
        reply = await logic.task_finished(chat_sender, msg.task, msg.steps, msg.action_ids)
    await ctx.send(chat_sender, to_message(reply))


guardian.include(chat, publish_manifest=True)

if __name__ == "__main__":
    guardian.run()
