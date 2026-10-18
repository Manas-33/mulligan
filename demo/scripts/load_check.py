"""Load check: 200 actions in one session, then a rewind that must finish in under 30 s with
verification ok and prod-db back on its starting checksum.

  uv --directory gateway run python ../demo/scripts/load_check.py [--actions 200]

Uses the first agent in MULLIGAN_AGENT_TOKENS. Every action updates one customer row (rows are hit
more than once, so the rewind also undoes repeated writes to the same row).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from mulligan.settings import REPO_ROOT, load_dotenv

for _key, _value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(_key, _value)

API = f"http://127.0.0.1:{os.environ.get('MULLIGAN_API_PORT', '8000')}"
MCP = f"http://127.0.0.1:{os.environ.get('MULLIGAN_MCP_PORT', '8001')}/mcp"
DASH = {"Authorization": f"Bearer {os.environ['MULLIGAN_DASHBOARD_TOKEN']}"}
AGENT_TOKEN = os.environ["MULLIGAN_AGENT_TOKENS"].split(",")[0].split(":", 1)[1].strip()
LIMIT_S = 30.0


async def make_actions(n: int) -> str:
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"}, timeout=httpx2.Timeout(30.0, read=60.0)
    ) as http:
        async with Client(streamable_http_client(MCP, http_client=http)) as client:
            action_id = ""
            started = time.monotonic()
            for i in range(n):
                result = await client.call_tool(
                    "pg_update_rows",
                    {
                        "table": "customers",
                        "filter": [{"column": "id", "op": "eq", "value": 1 + i % 120}],
                        "values": {"plan": f"load-{i}"},
                    },
                )
                content = result.structured_content or {}
                if content.get("status") != "executed":
                    sys.exit(f"action {i} failed: {result.content}")
                action_id = str(content["action_id"])
            print(f"{n} actions in {time.monotonic() - started:.1f}s")
            return action_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actions", type=int, default=200)
    n = parser.parse_args().actions
    with httpx.Client(base_url=API, headers=DASH, timeout=120) as api:
        before = api.get("/api/checksums?recompute=true").json()["postgres"]["hash"]
        last = asyncio.run(make_actions(n))
        session_id = api.get(f"/api/actions/{last}").json()["session_id"]
        started = time.monotonic()
        job = api.post(
            "/api/rewind", json={"scope": {"session_id": session_id}, "dry_run": False}
        ).json()
        while job["status"] == "running":
            time.sleep(0.25)
            job = api.get(f"/api/rewind/{job['id']}").json()
        took = time.monotonic() - started
        after = api.get("/api/checksums?recompute=true").json()["postgres"]["hash"]
    ok = (
        job["status"] == "completed"
        and job["verification"]["postgres"]["ok"]
        and after == before
        and took < LIMIT_S
    )
    print(
        f"rewind of {len(job['steps'])} steps: {job['status']} in {took:.1f}s "
        f"(limit {LIMIT_S:.0f}s), verification ok={job['verification']['postgres']['ok']}, "
        f"checksum restored={after == before}"
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
