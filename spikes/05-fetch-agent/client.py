"""Send one chat message to the spike agent through the Agentverse mailbox.

Run:  .venv/bin/python client.py [text]
Uses a throwaway seed and a local endpoint so replies come straight back here.
"""

import secrets
import sys
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Agent, Context
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
from uagents import Protocol

TARGET = "agent1qveg9ca3jn06upx2ztx78k40kjm4jxg2ywuul2lmfrvc77druqwp6qae9w4"
TEXT = " ".join(sys.argv[1:]) or "hello from the local test client"

client = Agent(
    name="spike05-client",
    seed=secrets.token_hex(16),
    port=8002,
    endpoint=["http://127.0.0.1:8002/submit"],
    publish_agent_details=False,
)
chat = Protocol(spec=chat_protocol_spec)


@client.on_event("startup")
async def send(ctx: Context):
    await ctx.send(TARGET, ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                                       content=[TextContent(type="text", text=TEXT)]))
    ctx.logger.info(f"sent {TEXT!r}")


@chat.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(f"ACK from {sender}")


@chat.on_message(ChatMessage)
async def on_reply(ctx: Context, sender: str, msg: ChatMessage):
    ctx.logger.info(f"REPLY content={[type(c).__name__ for c in msg.content]} "
                    f"text={''.join(c.text for c in msg.content if isinstance(c, TextContent))!r}")


client.include(chat)

if __name__ == "__main__":
    client.run()
