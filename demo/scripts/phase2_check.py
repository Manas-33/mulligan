"""Phase 2 acceptance check: an agent changes prod-db over MCP, then a rewind restores it.

usage (gateway and both DBs running, env from .env):
  uv --directory gateway run python ../demo/scripts/phase2_check.py

1. Every REST route and the MCP endpoint refuse a missing token; the MCP port serves only /mcp.
2. As the first agent in MULLIGAN_AGENT_TOKENS: list the tools, then delete inactive customers
   (cascades), zero a price and drop legacy_pricing through the typed tools.
3. Every call is logged with after-hashes, and POST /api/rewind for the session restores
   prod-db to the checksum it had before.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

API = f"http://127.0.0.1:{os.environ.get('MULLIGAN_API_PORT', '8000')}"
MCP = f"http://127.0.0.1:{os.environ.get('MULLIGAN_MCP_PORT', '8001')}/mcp"
DASH = {"Authorization": f"Bearer {os.environ['MULLIGAN_DASHBOARD_TOKEN']}"}
AGENT_TOKEN = os.environ["MULLIGAN_AGENT_TOKENS"].split(",")[0].split(":", 1)[1].strip()

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


async def agent_session() -> list[dict[str, object]]:
    calls = [
        ("pg_query", {"sql": "select count(*) from customers"}),
        (
            "pg_delete_rows",
            {
                "table": "customers",
                "filter": [{"column": "last_order_at", "op": "lt", "value": "2025-10-17T00:00:00Z"}],
            },
        ),
        (
            "pg_update_rows",
            {
                "table": "products",
                "filter": [{"column": "sku", "op": "eq", "value": "WID-001"}],
                "values": {"price": 0},
            },
        ),
        ("pg_drop_table", {"table": "legacy_pricing"}),
        ("mulligan_status", {}),
    ]
    results = []
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"}, timeout=httpx2.Timeout(30.0, read=120.0)
    ) as http:
        async with Client(streamable_http_client(MCP, http_client=http), mode="auto") as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            check({"pg_query", "pg_delete_rows", "pg_drop_table", "mulligan_status"} <= tools, f"tools listed: {sorted(tools)}")
            for name, args in calls:
                result = await client.call_tool(name, args)
                content = result.structured_content or {}
                print(f"      {name}: {result.content[0].text.splitlines()[0]}")
                check(not result.is_error and content.get("status") == "executed", f"{name} executed")
                results.append(content)
    return results


async def main() -> None:
    with httpx.Client(base_url=API, timeout=30) as api:
        for path in ["/api/sessions", "/api/actions", "/api/checksums", "/api/events"]:
            check(api.get(path).status_code == 401, f"{path} without a token -> 401")
        check(api.get("/healthz").status_code == 200, "/healthz is open")
        mcp_origin = MCP.rsplit("/", 1)[0]
        check(httpx.post(MCP, json={}).status_code == 401, "MCP without a token -> 401")
        check(httpx.get(mcp_origin + "/api/sessions", headers=DASH).status_code == 404, "MCP port doesn't serve /api")

        before = api.get("/api/checksums?recompute=true", headers=DASH).json()["postgres"]["hash"]
        results = await agent_session()
        session_id = api.get(f"/api/actions/{results[1]['action_id']}", headers=DASH).json()["session_id"]
        actions = api.get(f"/api/actions?session_id={session_id}", headers=DASH).json()
        check(len(actions) == 5, f"5 actions logged for {session_id}")
        writes = [a for a in actions if a["reversibility"]["label"] != "read_only"]
        check(all(a["after_hashes"] and a["inverse"] for a in writes), "writes have after_hashes and an inverse")
        after = api.get("/api/checksums?recompute=true", headers=DASH).json()["postgres"]["hash"]
        check(after != before, "prod-db changed")

        plan = api.post("/api/rewind", json={"scope": {"session_id": session_id}}, headers=DASH).json()
        check(plan["status"] == "planned" and len(plan["steps"]) == 3, "dry run plans 3 steps")
        started = time.monotonic()
        job = api.post(
            "/api/rewind", json={"scope": {"session_id": session_id}, "dry_run": False}, headers=DASH
        ).json()
        while job["status"] == "running":
            time.sleep(0.2)
            job = api.get(f"/api/rewind/{job['id']}", headers=DASH).json()
        print(f"      rewind {job['id']} {job['status']} in {time.monotonic() - started:.1f}s")
        for step in job["steps"]:
            print(f"      {step['tool']}: {step['result']} - {step['detail']}")
        check(job["status"] == "completed", "rewind completed")
        check(job["verification"]["postgres"]["ok"] is True, "verification ok")
        restored = api.get("/api/checksums?recompute=true", headers=DASH).json()["postgres"]["hash"]
        check(restored == before, "prod-db checksum equals the one before the session")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
