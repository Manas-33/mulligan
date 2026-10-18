"""A mock Slack MCP server Mulligan has never seen, for the rules-gate demo.

    uv --directory gateway run python ../demo/upstream/slack_server.py   # http://127.0.0.1:8200/mcp

Set UPSTREAM_MCP_SERVERS=slack=http://127.0.0.1:8200/mcp and the gateway exposes its tools as
upstream__slack__list_channels and upstream__slack__post_message. It keeps posts in memory and
prints each one, so you can see whether a held post was ever sent.
"""

from __future__ import annotations

import os
import time
from typing import Any

import uvicorn
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations

CHANNELS = [
    {"id": "C001", "name": "general", "members": 42},
    {"id": "C002", "name": "pricing", "members": 7},
    {"id": "C003", "name": "customers", "members": 15},
]
POSTS: list[dict[str, Any]] = []

slack = MCPServer("mock-slack", instructions="A tiny Slack workspace for Acme Widgets.")


@slack.tool(
    description="List the workspace's channels.",
    annotations=ToolAnnotations(read_only_hint=True),
)
def list_channels() -> dict[str, Any]:
    return {"channels": CHANNELS}


@slack.tool(
    description="Post a message to a channel. Everyone in the channel is notified.",
    annotations=ToolAnnotations(read_only_hint=False),
)
def post_message(channel: str, text: str) -> dict[str, Any]:
    post = {"channel": channel, "text": text, "ts": f"{time.time():.6f}"}
    POSTS.append(post)
    print(f"[mock-slack] posted to {channel}: {text}", flush=True)
    return {"ok": True, "ts": post["ts"]}


def app() -> Any:
    return slack.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )


if __name__ == "__main__":
    uvicorn.run(app(), host="127.0.0.1", port=int(os.environ.get("MOCK_SLACK_PORT", "8200")))
