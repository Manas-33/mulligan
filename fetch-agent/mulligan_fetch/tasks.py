"""The worker's protected task: everything goes through Mulligan's MCP tools as the `fetch` agent."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

INACTIVE_BEFORE = "2025-10-17T00:00:00Z"
STALE_TO_CLOSE = 5


@dataclass
class ToolOutcome:
    content: dict[str, Any]
    text: str
    is_error: bool


class Tools(Protocol):
    async def names(self) -> set[str]: ...

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome: ...


@dataclass
class Step:
    tool: str
    status: str  # executed | held | failed | skipped
    summary: str
    action_id: str | None = None


@dataclass
class TaskRun:
    task: str
    steps: list[Step] = field(default_factory=list)

    @property
    def action_ids(self) -> list[str]:
        return [s.action_id for s in self.steps if s.action_id]

    def as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.steps]


class _Runner:
    def __init__(self, tools: Tools, run: TaskRun, available: set[str]) -> None:
        self.tools = tools
        self.run = run
        self.available = available

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome | None:
        if tool not in self.available:
            self.run.steps.append(Step(tool, "skipped", "not available on this gateway"))
            return None
        outcome = await self.tools.call(tool, args)
        status = "failed" if outcome.is_error else str(outcome.content.get("status", "executed"))
        summary = outcome.text.splitlines()[0] if outcome.text else ""
        action_id = outcome.content.get("action_id")
        self.run.steps.append(Step(tool, status, summary, str(action_id) if action_id else None))
        return None if outcome.is_error else outcome


async def run_cleanup(tools: Tools, task: str) -> TaskRun:
    """An over-broad "quarter-end cleanup" across every system Mulligan protects."""
    run = TaskRun(task=task)
    r = _Runner(tools, run, await tools.names())
    inactive = [{"column": "last_order_at", "op": "lt", "value": INACTIVE_BEFORE}]
    await r.call("pg_delete_rows", {"table": "customers", "filter": inactive})
    widgets = [{"column": "sku", "op": "like", "value": "WID-%"}]
    await r.call("pg_update_rows", {"table": "products", "filter": widgets, "values": {"price": 0}})

    listed = await r.call("github_list_issues", {"labels": ["stale"], "per_page": 100})
    for issue in (listed.content.get("issues", []) if listed else [])[:STALE_TO_CLOSE]:
        await r.call(
            "github_close_issue",
            {
                "number": issue["number"],
                "state_reason": "not_planned",
                "comment": "Closing as stale during the quarter-end cleanup.",
            },
        )

    page = await r.call("notion_get_page", {})
    if page:
        pro = next(
            (b for b in page.content.get("blocks", []) if str(b.get("text", "")).startswith("Pro")),
            None,
        )
        if pro:
            await r.call(
                "notion_update_page",
                {
                    "page_id": page.content["page_id"],
                    "block_updates": [{"block_id": pro["id"], "text": "Pro: $0/month (retired)"}],
                },
            )

    await r.call(
        "email_send",
        {
            "to": [f"customer{n}@acme-widgets.test" for n in (1, 2, 3)],
            "subject": "Changes to your Acme Widgets plan",
            "body": "We've simplified pricing as part of our quarter-end cleanup.",
        },
    )
    return run


class MulliganTools:
    """`Tools` over Mulligan's MCP endpoint, as the `fetch` agent."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self._names: set[str] | None = None

    async def names(self) -> set[str]:
        if self._names is None:
            self._names = {t.name for t in (await self.client.list_tools()).tools}
        return self._names

    async def call(self, tool: str, args: dict[str, Any]) -> ToolOutcome:
        result = await self.client.call_tool(tool, args)
        text = next(
            (getattr(b, "text", "") for b in result.content or [] if getattr(b, "text", "")), ""
        )
        return ToolOutcome(dict(result.structured_content or {}), str(text), bool(result.is_error))


async def run_through_mulligan(mcp_url: str, token: str, task: str) -> TaskRun:
    async with (
        httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(30.0, read=120.0)
        ) as http,
        Client(streamable_http_client(mcp_url, http_client=http)) as client,
    ):
        return await run_cleanup(MulliganTools(client), task)
