"""Check an MCP URL the way an agent sees it.

usage: uv run --env-file .env python client_check.py <mcp-url>

- lists and calls `whoami` with the agent token as a legacy client and as an auto-negotiating one
- with MULLIGAN_FORCE_HANDSHAKE=1 (default) auto falls back to the handshake and a pinned 2026-07-28 client is
  refused; with it off, auto speaks 2026-07-28 and no Mcp-Session-Id exists
- an MCP request without a token, or with a wrong one, gets 401
- nothing but /mcp answers on that origin (REST routes, docs, OAuth metadata all 404)
"""

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit

import httpx2

from mcp import Client
from mcp.client.streamable_http import streamable_http_client

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "check", "version": "0"}},
}
MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
OTHER_PATHS = ["/healthz", "/api/calls", "/docs", "/openapi.json", "/.well-known/oauth-protected-resource/mcp"]

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


async def call_whoami(url: str, token: str, mode: str) -> None:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, read=300.0)
    ) as http:
        async with Client(streamable_http_client(url, http_client=http), mode=mode) as client:
            tools = [t.name for t in (await client.list_tools()).tools]
            result = await client.call_tool("whoami", {})
            seen = json.loads(result.content[0].text)  # a bare `-> dict` tool returns text, not structured_content
            protocol = client.protocol_version
    print(f"      mode={mode} protocol={protocol} tools={tools}")
    print(f"      server saw {json.dumps(seen)}")
    check("whoami" in tools and seen.get("agent") == "spike-agent", f"[{mode}] lists and calls whoami as spike-agent")
    if protocol == "2026-07-28":
        check(seen.get("mcp_session_id") is None, f"[{mode}] 2026-07-28 request carries no Mcp-Session-Id")
    else:
        check(bool(seen.get("mcp_session_id")), f"[{mode}] server sees the Mcp-Session-Id")


async def main(url: str) -> None:
    agent_token = os.environ["SPIKE_AGENT_TOKEN"]
    api_token = os.environ["SPIKE_API_TOKEN"]
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))

    for mode in ("legacy", "auto"):
        await call_whoami(url, agent_token, mode)
    try:
        await call_whoami(url, agent_token, "2026-07-28")
    except Exception as e:  # noqa: BLE001 - the forced-handshake server refuses a pinned modern client
        print(f"      mode=2026-07-28 refused: {type(e).__name__}: {e}")

    async with httpx2.AsyncClient(timeout=30.0) as http:
        r = await http.post(url, json=INIT, headers=MCP_HEADERS)
        check(r.status_code == 401, f"MCP without token -> {r.status_code}")
        r = await http.post(url, json=INIT, headers={**MCP_HEADERS, "authorization": "Bearer wrong"})
        check(r.status_code == 401, f"MCP with a wrong token -> {r.status_code}")
        for path in OTHER_PATHS:
            r = await http.get(origin + path, headers={"authorization": f"Bearer {api_token}"})
            check(r.status_code == 404, f"{path} on the MCP origin (with the API token) -> {r.status_code}")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
