from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from mulligan_fetch import mulligan_api
from mulligan_fetch.config import agent_token
from mulligan_fetch.mulligan_api import MulliganAPI, MulliganError
from mulligan_fetch.tasks import STALE_TO_CLOSE, ToolOutcome, run_cleanup


def test_agent_token() -> None:
    assert agent_token("claude_code:a, fetch:b,rogue:c", "fetch") == "b"
    assert agent_token("claude_code:a", "fetch") == ""


async def test_api_sends_the_service_token_and_asi_one_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mulligan_api, "POLL_SECONDS", 0)
    seen: list[httpx.Request] = []
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/decision"):
            return httpx.Response(
                200, json={"id": "act_1", "status": "approved", "tool": "email_send"}
            )
        if request.method == "POST" and request.url.path == "/api/rewind":
            return httpx.Response(202, json={"id": "rwd_1", "status": "running", "steps": []})
        if request.url.path == "/api/rewind/rwd_1":
            polls["n"] += 1
            return httpx.Response(
                200,
                json={
                    "id": "rwd_1",
                    "status": "running" if polls["n"] < 2 else "completed",
                    "steps": [],
                },
            )
        return httpx.Response(404, json={"detail": "not found"})

    client = httpx.AsyncClient(base_url="http://gw", transport=httpx.MockTransport(handler))
    api = MulliganAPI("http://gw", "svc-token", client)
    await api.decide("act_1", "approve", "4821", "asi_one:agent1q")
    body = json.loads(seen[-1].content)
    assert body == {
        "decision": "approve",
        "code": "4821",
        "channel": "asi_one",
        "decided_by": "asi_one:agent1q",
    }
    assert seen[-1].headers["X-Mulligan-Service-Token"] == "svc-token"
    job = await api.rewind("ses_1")
    assert job["status"] == "completed" and polls["n"] == 2
    assert json.loads(seen[-3].content)["scope"] == {"session_id": "ses_1"}
    with pytest.raises(MulliganError, match="404"):
        await api.action("act_missing")


class FakeTools:
    def __init__(self, names: set[str]) -> None:
        self._names = names
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def names(self) -> set[str]:
        return self._names

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        self.calls.append((tool, args))
        n = len(self.calls)
        if tool == "github_list_issues":
            issues = [{"number": i} for i in (1, 2, 3, 4, 6, 7, 8)]
            return ToolOutcome(
                {"issues": issues, "action_id": f"act_{n}", "status": "executed"}, "Listed 7", False
            )
        if tool == "notion_get_page":
            blocks = [
                {"id": "b1", "text": "Starter: $29/month"},
                {"id": "b2", "text": "Pro: $99/month"},
            ]
            return ToolOutcome(
                {"page_id": "p1", "blocks": blocks, "action_id": f"act_{n}", "status": "executed"},
                "Read",
                False,
            )
        if tool == "email_send":
            return ToolOutcome(
                {"action_id": f"act_{n}", "status": "held"}, "Held for approval: Email", False
            )
        return ToolOutcome({"action_id": f"act_{n}", "status": "executed"}, f"{tool} done", False)


ALL = {
    "pg_delete_rows",
    "pg_update_rows",
    "github_list_issues",
    "github_close_issue",
    "notion_get_page",
    "notion_update_page",
    "email_send",
}


async def test_cleanup_runs_every_system_through_mulligan() -> None:
    tools = FakeTools(ALL)
    run = await run_cleanup(tools, "quarter-end cleanup")
    used = [t for t, _ in tools.calls]
    assert used.count("github_close_issue") == STALE_TO_CLOSE
    closed = [a["number"] for t, a in tools.calls if t == "github_close_issue"]
    assert closed == [1, 2, 3, 4, 6]
    update = next(a for t, a in tools.calls if t == "notion_update_page")
    assert update == {
        "page_id": "p1",
        "block_updates": [{"block_id": "b2", "text": "Pro: $0/month (retired)"}],
    }
    assert [s.status for s in run.steps][-1] == "held"
    assert len(run.action_ids) == len(tools.calls)


async def test_cleanup_skips_tools_the_gateway_doesnt_expose() -> None:
    tools = FakeTools({"pg_delete_rows", "email_send"})
    run = await run_cleanup(tools, "cleanup")
    assert [t for t, _ in tools.calls] == ["pg_delete_rows", "email_send"]
    skipped = {s.tool for s in run.steps if s.status == "skipped"}
    assert {"pg_update_rows", "github_list_issues", "notion_get_page"} <= skipped
