"""The demo's rogue cleanup agent.

  --replay <file>  Send a recording's calls through Mulligan over MCP with its timing (the demo
                   fallback). Uses the token of the agent named `rogue_replay` in
                   MULLIGAN_AGENT_TOKENS, so the session shows as mode=replay.
  --live           Run a real LLM agent with only Mulligan's tools (see rogue_live.py) and record
                   every call to demo/recordings/<timestamp>.json.

Tools a recording uses but the gateway doesn't expose (an adapter that isn't configured) are
skipped with a note, so a recording still replays on a partial stack.

  uv --directory gateway run python ../demo/rogue_agent.py --replay ../demo/recordings/scripted.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mulligan.settings import REPO_ROOT, load_dotenv

for _key, _value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(_key, _value)

HERE = Path(__file__).resolve().parent
RECORDINGS = HERE / "recordings"


def mcp_url() -> str:
    return os.environ.get("MULLIGAN_MCP_URL") or (
        f"http://127.0.0.1:{os.environ.get('MULLIGAN_MCP_PORT', '8001')}/mcp"
    )


def token_for(agent_id: str) -> str:
    for item in os.environ.get("MULLIGAN_AGENT_TOKENS", "").split(","):
        name, _, token = item.strip().partition(":")
        if name == agent_id and token:
            return token
    sys.exit(f"MULLIGAN_AGENT_TOKENS has no entry for {agent_id!r}")


def first_line(result: Any) -> str:
    for block in result.content or []:
        text = getattr(block, "text", "")
        if text:
            return str(text).splitlines()[0]
    return ""


async def replay(path: Path, speed: float) -> None:
    recording = json.loads(path.read_text())
    calls = recording["calls"]
    print(f"replaying {path.name}: {len(calls)} calls at {speed}x")
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token_for('rogue_replay')}"},
        timeout=httpx2.Timeout(30.0, read=120.0),
    ) as http:
        async with Client(streamable_http_client(mcp_url(), http_client=http)) as client:
            available = {t.name for t in (await client.list_tools()).tools}
            started = time.monotonic()
            for call in calls:
                wait = call.get("t_ms", 0) / 1000 / speed - (time.monotonic() - started)
                if wait > 0:
                    await asyncio.sleep(wait)
                tool, args = call["tool"], call.get("args", {})
                if tool not in available:
                    print(f"  skip  {tool} (not exposed by this gateway)")
                    continue
                result = await client.call_tool(tool, args)
                status = (result.structured_content or {}).get("status", "error")
                print(f"  {status:<9}{tool}: {first_line(result)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path, metavar="FILE")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--speed", type=float, default=1.0, help="replay speed multiplier")
    args = parser.parse_args()
    if args.replay:
        asyncio.run(replay(args.replay, args.speed))
    else:
        from rogue_live import run_live  # noqa: PLC0415 (needs the LLM SDK only in live mode)

        asyncio.run(run_live(RECORDINGS))


if __name__ == "__main__":
    main()
