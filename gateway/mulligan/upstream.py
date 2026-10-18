"""MCP servers placed behind Mulligan (UPSTREAM_MCP_SERVERS). Their tools are exposed to agents as
upstream__<server>__<tool>, labelled by the rules gate, logged, and never undone."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

from mcp import Client

from mulligan.interfaces import (
    Adapter,
    Capture,
    ToolAnnotations,
    ToolCall,
    ToolResult,
    UpstreamToolSchema,
)
from mulligan.models import Action, Inverse, RewindStep, StepResult, System

log = logging.getLogger("mulligan.upstream")

SERVER_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,15}$")
MAX_TOOL_NAME = 64
PREFIX = "upstream"

# How to reach a server: a URL, or (in tests) anything mcp.Client accepts, such as an MCPServer.
Target = Any


def parse_servers(spec: str) -> dict[str, str]:
    """`slack=http://127.0.0.1:8200/mcp,docs=...` -> {name: url}."""
    servers: dict[str, str] = {}
    for item in filter(None, (p.strip() for p in spec.split(","))):
        name, sep, url = item.partition("=")
        name = name.strip()
        if not sep or not url.strip():
            raise ValueError("UPSTREAM_MCP_SERVERS entries look like <name>=<url>")
        if not SERVER_NAME.match(name) or "__" in name:
            raise ValueError(f"bad upstream server name {name!r}")
        servers[name] = url.strip()
    return servers


def exposed_name(server: str, tool: str) -> str:
    return f"{PREFIX}__{server}__{tool}"


def _text(result: Any) -> str:
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            return str(text)
    return ""


class UpstreamAdapter(Adapter):
    """Forwards calls to upstream servers. Upstream calls can't be undone."""

    system: ClassVar[System] = System.upstream
    tools: ClassVar[frozenset[str]] = frozenset()  # dynamic; see `schemas`

    def __init__(
        self, servers: Mapping[str, Target], connect: Callable[[Target], Client] | None = None
    ) -> None:
        self.servers = dict(servers)
        self.connect = connect or (lambda target: Client(target, mode="auto"))
        self.schemas: dict[str, UpstreamToolSchema] = {}

    async def discover(self) -> dict[str, UpstreamToolSchema]:
        """List every server's tools. A server that can't be reached is skipped with a warning."""
        for server, target in self.servers.items():
            try:
                async with self.connect(target) as client:
                    listed = (await client.list_tools()).tools
            except Exception:  # noqa: BLE001 (one bad upstream shouldn't stop the gateway)
                log.warning(
                    "upstream %s unreachable; its tools are not exposed", server, exc_info=True
                )
                continue
            for tool in listed:
                name = exposed_name(server, tool.name)
                if len(name) > MAX_TOOL_NAME:
                    log.warning("skipping %s: exposed name is over %d chars", name, MAX_TOOL_NAME)
                    continue
                hints = tool.annotations
                self.schemas[name] = UpstreamToolSchema(
                    server=server,
                    name=tool.name,
                    exposed_name=name,
                    description=tool.description,
                    input_schema=dict(tool.input_schema or {}),
                    annotations=ToolAnnotations(
                        title=hints.title,
                        readOnlyHint=hints.read_only_hint,
                        destructiveHint=hints.destructive_hint,
                        idempotentHint=hints.idempotent_hint,
                        openWorldHint=hints.open_world_hint,
                    )
                    if hints
                    else None,
                )
        return dict(self.schemas)

    async def capture_before(self, call: ToolCall) -> Capture:
        return Capture()

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        schema = self.schemas[call.tool]
        async with self.connect(self.servers[schema.server]) as client:
            result = await client.call_tool(schema.name, call.args)
        text = _text(result)
        content = dict(result.structured_content or {}) or ({"text": text} if text else {})
        summary = f"{schema.server}.{schema.name}" + (f": {text[:200]}" if text else "")
        return ToolResult(
            structured_content=content, summary=summary, is_error=bool(result.is_error)
        )

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        return Capture()

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        return None

    async def plan_revert(self, action: Action) -> RewindStep:
        return await self.revert(action)

    async def revert(self, action: Action) -> RewindStep:
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=StepResult.skipped,
            kept_fields=[],
            changes=[],
            changes_truncated=False,
            detail="Upstream calls can't be undone.",
        )

    async def checksum(self) -> str | None:
        return None
