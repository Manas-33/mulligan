# /// script
# requires-python = "==3.12.*"
# dependencies = ["sentry-sdk==2.72.0", "mcp==2.3.0", "fastapi==0.143.3", "uvicorn==0.54.0"]
# ///
"""Spike 0.8: Sentry MCP server monitoring on the gateway stack.

Starts FastAPI with an mcp v2 `MCPServer` mounted at /mcp (Streamable HTTP) on 127.0.0.1:8001,
initializes Sentry with `MCPIntegration` (SENTRY_DSN_GATEWAY), calls a tool with the real MCP
client, then checks the span Sentry is about to send and flushes it to the mulligan-gateway project.

Pass: a `tools/call <name>` span with op `mcp.server`, the tool name, the MCP request id and
transport, and no tool arguments in the payload. (MCP protocol 2026-07-28, which mcp 2.x speaks,
is sessionless, so there is no mcp-session-id to attach; the session id is reported, not required.)

Run from the repo root:  uv run spikes/08-sentry-mcp/spike.py
"""

import asyncio
import contextlib
import sys
import threading
import time
from pathlib import Path

import httpx2
import sentry_sdk
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from sentry_sdk.integrations.mcp import MCPIntegration

ROOT = Path(__file__).resolve().parents[2]
PORT = 8001
TOOL = "pg_query"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        env[key.strip()] = value.split("#", 1)[0].strip()
    return env


captured: list[dict] = []


def scrub_and_keep(event, _hint):
    # MCPIntegration attaches every tool argument as mcp.request.argument.* regardless of
    # send_default_pii (that flag only gates results/prompts). Agent arguments can hold
    # customer data, so the gateway strips them before anything leaves the process.
    for span in [*(event.get("spans") or []), event.get("contexts", {}).get("trace") or {}]:
        data = span.get("data") or {}
        for key in [k for k in data if k.startswith("mcp.request.argument.")]:
            data[key] = "[scrubbed]"
    captured.append(event)
    return event


env = load_env(ROOT / ".env")
dsn = env.get("SENTRY_DSN_GATEWAY", "")
if not dsn:
    print("SENTRY_DSN_GATEWAY missing in .env")
    sys.exit(2)

sentry_sdk.init(
    dsn=dsn,
    environment=env.get("SENTRY_ENVIRONMENT", "dev"),
    release="mulligan-spike-0.8",
    traces_sample_rate=1.0,
    send_default_pii=False,  # tool args/results stay out of Sentry unless we opt in
    integrations=[MCPIntegration()],
    before_send_transaction=scrub_and_keep,
)

mcp = MCPServer("mulligan-spike")


@mcp.tool()
def pg_query(sql: str) -> list[dict]:
    """Run a read-only query (stub: returns two fake rows)."""
    return [{"id": 1, "name": "Acme"}, {"id": 2, "name": "Globex"}]


mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=False)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


app.mount("/", mcp_app)


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f": {detail}" if detail else ""))
    return ok


async def call_tool() -> str:
    headers = {"Authorization": "Bearer spike-agent-token"}
    async with httpx2.AsyncClient(headers=headers) as http:
        async with Client(streamable_http_client(f"http://127.0.0.1:{PORT}/mcp", http_client=http)) as client:
            tools = await client.list_tools()
            assert TOOL in [t.name for t in tools.tools], tools
            result = await client.call_tool(TOOL, {"sql": "select id, name from customers limit 2"})
            return str(result.structured_content)


def main() -> int:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    results: list[bool] = []
    print("Calling the tool over Streamable HTTP...")
    out = asyncio.run(call_tool())
    results.append(check("tool returned rows", "Acme" in out, out[:60]))

    sentry_sdk.flush(timeout=10)
    server.should_exit = True
    thread.join(timeout=5)

    spans = [s for e in captured for s in (e.get("spans") or [])] + [
        {**(e.get("contexts", {}).get("trace") or {}), "description": e.get("transaction")} for e in captured
    ]
    mcp_spans = [s for s in spans if s.get("op") == "mcp.server"]
    tool_span = next((s for s in mcp_spans if (s.get("description") or "").startswith(f"tools/call {TOOL}")), None)
    results.append(check("transactions captured", bool(captured), f"{len(captured)} sent"))
    results.append(check("mcp.server spans", bool(mcp_spans), ", ".join(sorted({s.get('description') or '?' for s in mcp_spans}))))
    results.append(check(f"tools/call {TOOL} span", tool_span is not None))
    if tool_span:
        data = tool_span.get("data") or {}
        mcp_keys = sorted(k for k in data if k.startswith("mcp."))
        print(f"  span data keys: {mcp_keys}")
        results.append(check("mcp.tool.name", data.get("mcp.tool.name") == TOOL, str(data.get("mcp.tool.name"))))
        results.append(check("mcp.request.id", bool(data.get("mcp.request.id")), str(data.get("mcp.request.id"))))
        session = data.get("mcp.session.id") or data.get("mcp.session_id")
        print(f"  mcp session id: {session or 'none (sessionless protocol 2026-07-28)'}")
        results.append(check("transport is http", data.get("mcp.transport") == "http", str(data.get("mcp.transport"))))
        args = {k: v for k, v in data.items() if k.startswith("mcp.request.argument.")}
        results.append(check("tool args scrubbed before send", bool(args) and set(args.values()) == {"[scrubbed]"}, str(args)))
        print(f"  trace id: {tool_span.get('trace_id')}  (search it in Sentry: Explore > Traces)")

    passed = all(results)
    print(f"\nSpike 0.8: {'PASS' if passed else 'FAIL'} ({sum(results)}/{len(results)} checks), sentry-sdk {sentry_sdk.VERSION}, mcp 2.3.0")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
