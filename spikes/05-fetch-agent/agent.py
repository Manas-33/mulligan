"""Spike 0.5: a uAgents chat-protocol agent reachable from ASI:One.

Run:  .venv/bin/python agent.py
Reads GUARDIAN_AGENT_SEED and ASI_ONE_API_KEY from the environment, falling
back to the main checkout's .env (path in MULLIGAN_ENV_FILE or ../../.env).

Say "card" in chat to get a review-kind interactive card instead of text.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from openai import OpenAI
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

HERE = Path(__file__).resolve().parent


def load_env() -> None:
    candidates = [os.environ.get("MULLIGAN_ENV_FILE"), HERE / "../../.env",
                  "/Users/dalvi/Downloads/Projects/rewind/.env"]
    for path in candidates:
        if path and Path(path).is_file():
            for line in Path(path).read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return


load_env()

llm = (OpenAI(base_url="https://api.asi1.ai/v1", api_key=os.environ["ASI_ONE_API_KEY"])
       if os.environ.get("ASI_ONE_API_KEY") else None)

agent = Agent(
    name="mulligan-guardian",
    seed=os.environ["GUARDIAN_AGENT_SEED"],
    port=int(os.environ.get("SPIKE_AGENT_PORT", "8001")),
    mailbox=True,
    publish_agent_details=True,
    readme_path=str(HERE / "README.md"),
    description="Runs agent tasks through the Mulligan gateway and rewinds them on request.",
)

chat = Protocol(spec=chat_protocol_spec)


def now() -> datetime:
    return datetime.now(timezone.utc)


def text_reply(text: str) -> ChatMessage:
    return ChatMessage(timestamp=now(), msg_id=uuid4(),
                       content=[TextContent(type="text", text=text),
                                EndSessionContent(type="end-session")])


def review_card() -> ChatMessage:
    payload = {
        "title": "Rewind session sess_demo?",
        "summary_rows": [
            {"label": "Actions", "value": "3 (Postgres, GitHub, Notion)"},
            {"label": "Held for approval", "value": "1"},
        ],
        "approve_cta": {"label": "Rewind", "selection": {"action": "rewind", "session_id": "sess_demo"}, "primary": True},
        "reject_cta": {"label": "Keep", "selection": {"action": "keep", "session_id": "sess_demo"}},
    }
    return ChatMessage(timestamp=now(), msg_id=uuid4(), content=[
        TextContent(type="text", text="Here's the session. Rewind it?"),
        MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1",
            "requires_card_interaction": "true",
            "card_kind": "review",
            "card_payload": json.dumps(payload),
        }),
    ])


def word_reply(user_text: str) -> str:
    fallback = f"Mulligan Guardian (spike) heard: {user_text!r}"
    if llm is None:
        return fallback
    try:
        result = llm.chat.completions.create(
            model="asi1-mini",
            messages=[
                {"role": "system", "content": (
                    "You are Mulligan Guardian, an agent that runs tasks through the Mulligan "
                    "gateway and can rewind them. This is a connectivity test: reply in one "
                    "or two friendly sentences and confirm you received the message.")},
                {"role": "user", "content": user_text},
            ],
            max_tokens=200,
        )
        return str(result.choices[0].message.content)
    except Exception:
        return fallback


@chat.on_message(ChatMessage)
async def handle_message(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=now(), acknowledged_msg_id=msg.msg_id))
    kinds = [type(item).__name__ for item in msg.content]
    user_text = "".join(item.text for item in msg.content if isinstance(item, TextContent)).strip()
    # ASI:One keeps the "@agent1..." mention in the text it forwards.
    user_text = re.sub(r"@agent1[0-9a-z]+", "", user_text).strip()
    ctx.logger.info(f"chat from {sender}: content={kinds} text={user_text[:200]!r}")
    if not user_text:
        # StartSessionContent-only messages carry no text; nothing to answer yet.
        if any(isinstance(item, StartSessionContent) for item in msg.content):
            return
    if user_text.lower().rstrip(".!") in {"card", "show card", "show me a card"}:
        await ctx.send(sender, review_card())
        return
    await ctx.send(sender, text_reply(word_reply(user_text)))


@chat.on_message(ChatAcknowledgement)
async def handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ack from {sender} for {msg.acknowledged_msg_id}")


agent.include(chat, publish_manifest=True)

if __name__ == "__main__":
    agent.run()
