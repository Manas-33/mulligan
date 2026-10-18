"""The MCP server agents connect to: /mcp on port 8001 (Streamable HTTP), per-agent bearer auth.

Every tool call goes through `Gateway.call_tool`, which classifies, holds or executes it and logs
it. See contracts/mcp_tools.md for what each tool takes and returns.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.tools.base import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS
from pydantic import AnyHttpUrl, BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from mulligan.core import Gateway
from mulligan.interfaces import ToolResult, UpstreamToolSchema
from mulligan.models import Session
from mulligan.settings import AgentToken, Settings

log = logging.getLogger("mulligan.mcp")

IDLE_SECONDS = 30 * 60
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
TUNNEL_SUFFIX = ".trycloudflare.com"


class AgentTokenVerifier(TokenVerifier):
    def __init__(self, tokens: dict[str, AgentToken]) -> None:
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        for known, entry in self.tokens.items():
            if hmac.compare_digest(token.encode(), known.encode()):
                return AccessToken(token=token, client_id=entry.agent.id, scopes=[])
        return None


class SessionMap:
    """Mulligan sessions for MCP sessions, or one per agent token for sessionless clients."""

    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway
        self._fallback: dict[str, tuple[Session, float]] = {}

    async def resolve(self, entry: AgentToken, mcp_session_id: str | None) -> Session:
        if mcp_session_id:
            key = f"{entry.agent.id}:{mcp_session_id}"
            return await self.gateway.start_session(entry.agent, entry.mode, key)
        now = time.monotonic()
        current = self._fallback.get(entry.agent.id)
        if current and now - current[1] < IDLE_SECONDS:
            self._fallback[entry.agent.id] = (current[0], now)
            return current[0]
        if current:
            await self.gateway.end_session(current[0].id)
        session = await self.gateway.start_session(entry.agent, entry.mode)
        self._fallback[entry.agent.id] = (session, now)
        return session


def to_result(result: ToolResult) -> CallToolResult:
    content = result.structured_content
    text = result.summary
    if content and not result.is_error:
        text += "\n" + json.dumps(content, default=str)[:20_000]
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=content,
        is_error=result.is_error,
    )


# --- tool inputs (contracts/mcp_tools.md) -----------------------------------------------------


class Filter(BaseModel):
    column: str
    op: Literal[
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "like",
        "ilike",
        "is_null",
        "is_not_null",
    ]
    value: Any = Field(
        default=None, description="Not used by is_null / is_not_null. A list for in / not_in."
    )


Table: Any = Field(description="Table name, or schema.table. Schema defaults to public.")
Filters: Any = Field(default_factory=list, description="Conditions, ANDed. Empty needs all=true.")
All: Any = Field(default=False, description="Set true to touch every row when the filter is empty.")


class BlockUpdate(BaseModel):
    block_id: str
    text: str = Field(description="New plain text. Table rows: cells separated by ' | '.")


class AppendBlock(BaseModel):
    type: Literal[
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "callout",
        "code",
    ]
    text: str


def build_mcp(settings: Settings, gateway: Gateway) -> tuple[MCPServer, AgentTokenVerifier]:
    verifier = AgentTokenVerifier(dict(settings.agent_tokens))
    mcp = MCPServer(
        "mulligan",
        instructions=(
            "Mulligan protects the systems you work on. Every call is recorded and can be undone. "
            "Writes that can't be undone (email, payments, unknown tools) wait for a human's "
            "approval: those calls return status 'held' and run only if approved."
        ),
        token_verifier=verifier,
        # No OAuth metadata route and no resource_metadata in the 401: agents use a bearer token.
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"http://127.0.0.1:{settings.mcp_port}"), resource_server_url=None
        ),
    )
    sessions = SessionMap(gateway)
    tokens = dict(settings.agent_tokens)

    async def run(tool: str, args: dict[str, Any], ctx: Context) -> CallToolResult:
        access = get_access_token()
        entry = next((e for t, e in tokens.items() if access and t == access.token), None)
        if entry is None:  # the verifier already let it through, so this shouldn't happen
            return to_result(
                ToolResult(structured_content={}, summary="unknown agent", is_error=True)
            )
        headers = ctx.headers or {}
        session = await sessions.resolve(entry, headers.get("mcp-session-id"))
        clean = {k: v for k, v in args.items() if v is not None}
        return to_result(await gateway.call_tool(session, tool, clean))

    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=True)
    builtin = gateway.builtin_tools | {"email_send", "payments_charge"}

    def tool(name: str, description: str, annotations: ToolAnnotations) -> Any:
        def register(fn: Any) -> Any:
            if name in builtin:
                mcp.tool(name=name, description=description, annotations=annotations)(fn)
            return fn

        return register

    @tool("pg_query", "Run one read-only SELECT on the company database. Results are capped.", read)
    async def pg_query(
        sql: str, ctx: Context, max_rows: int = Field(default=200, ge=1, le=1000)
    ) -> CallToolResult:
        return await run("pg_query", {"sql": sql, "max_rows": max_rows}, ctx)

    @tool("pg_insert_rows", "Insert rows into a table. Every row needs the same columns.", write)
    async def pg_insert_rows(
        ctx: Context,
        table: str = Table,
        rows: list[dict[str, Any]] = Field(min_length=1, max_length=1000),
    ) -> CallToolResult:
        return await run("pg_insert_rows", {"table": table, "rows": rows}, ctx)

    @tool("pg_update_rows", "Set column values on the rows that match a filter.", write)
    async def pg_update_rows(
        ctx: Context,
        table: str = Table,
        values: dict[str, Any] = Field(min_length=1, description="Column -> new value."),
        filter: list[Filter] = Filters,  # noqa: A002 (the contract's name)
        all: bool = All,  # noqa: A002
    ) -> CallToolResult:
        args = {
            "table": table,
            "values": values,
            "filter": [f.model_dump() for f in filter],
            "all": all,
        }
        return await run("pg_update_rows", args, ctx)

    @tool("pg_delete_rows", "Delete the rows that match a filter. FK cascades are followed.", write)
    async def pg_delete_rows(
        ctx: Context,
        table: str = Table,
        filter: list[Filter] = Filters,  # noqa: A002
        all: bool = All,  # noqa: A002
    ) -> CallToolResult:
        args = {"table": table, "filter": [f.model_dump() for f in filter], "all": all}
        return await run("pg_delete_rows", args, ctx)

    @tool("pg_drop_table", "Drop a table.", write)
    async def pg_drop_table(ctx: Context, table: str = Table) -> CallToolResult:
        return await run("pg_drop_table", {"table": table}, ctx)

    @tool("github_list_issues", "List issues in the company repository, newest first.", read)
    async def github_list_issues(
        ctx: Context,
        state: Literal["open", "closed", "all"] = "open",
        labels: list[str] | None = Field(
            default=None, description="Only issues with all of these."
        ),
        since: str | None = Field(
            default=None, description="Only issues updated after (ISO 8601)."
        ),
        per_page: int = Field(default=30, ge=1, le=100),
        page: int = Field(default=1, ge=1),
    ) -> CallToolResult:
        args = {
            "state": state,
            "labels": labels,
            "since": since,
            "per_page": per_page,
            "page": page,
        }
        return await run("github_list_issues", args, ctx)

    @tool("github_close_issue", "Close an issue, optionally with a comment.", write)
    async def github_close_issue(
        ctx: Context,
        number: int = Field(ge=1),
        state_reason: Literal["completed", "not_planned"] = "completed",
        comment: str | None = Field(default=None, description="Posted before closing."),
    ) -> CallToolResult:
        args = {"number": number, "state_reason": state_reason, "comment": comment}
        return await run("github_close_issue", args, ctx)

    @tool("github_comment", "Comment on an issue.", write)
    async def github_comment(
        ctx: Context, number: int = Field(ge=1), body: str = Field(min_length=1)
    ) -> CallToolResult:
        return await run("github_comment", {"number": number, "body": body}, ctx)

    @tool("github_update_file", "Create or replace a file in the repository (one commit).", write)
    async def github_update_file(
        ctx: Context,
        path: str = Field(min_length=1, description="Path in the repository, e.g. docs/a.md"),
        content: str = Field(description="The whole new file, UTF-8."),
        message: str = Field(min_length=1, description="Commit message."),
        branch: str | None = Field(default=None, description="Defaults to the default branch."),
    ) -> CallToolResult:
        args = {"path": path, "content": content, "message": message, "branch": branch}
        return await run("github_update_file", args, ctx)

    @tool("notion_get_page", "Read a Notion page: title, properties and blocks.", read)
    async def notion_get_page(
        ctx: Context,
        page_id: str | None = Field(default=None, description="Defaults to the pricing page."),
    ) -> CallToolResult:
        return await run("notion_get_page", {"page_id": page_id}, ctx)

    @tool(
        "notion_update_page",
        "Edit a Notion page: set properties, replace blocks' text (type and id are kept) and"
        " append blocks at the end. Give at least one of the three.",
        write,
    )
    async def notion_update_page(
        ctx: Context,
        page_id: str,
        properties: dict[str, Any] | None = Field(
            default=None,
            description="Property name -> value: text as a string, number, select as a string,"
            " multi-select as a list of strings, checkbox, date as an ISO string.",
        ),
        block_updates: list[BlockUpdate] | None = None,
        append_blocks: list[AppendBlock] | None = None,
    ) -> CallToolResult:
        args = {
            "page_id": page_id,
            "properties": properties,
            "block_updates": [b.model_dump() for b in block_updates or []] or None,
            "append_blocks": [b.model_dump() for b in append_blocks or []] or None,
        }
        return await run("notion_update_page", args, ctx)

    @tool("notion_trash_page", "Move a Notion page to the trash.", write)
    async def notion_trash_page(ctx: Context, page_id: str) -> CallToolResult:
        return await run("notion_trash_page", {"page_id": page_id}, ctx)

    @tool("email_send", "Send an email. Waits for a human's approval before it is sent.", write)
    async def email_send(
        ctx: Context,
        to: list[str] = Field(min_length=1, max_length=1000),
        subject: str = Field(min_length=1),
        body: str = Field(min_length=1),
        cc: list[str] | None = None,
    ) -> CallToolResult:
        args = {"to": to, "subject": subject, "body": body, "cc": cc}
        return await run("email_send", args, ctx)

    @tool("payments_charge", "Charge a customer. Waits for a human's approval.", write)
    async def payments_charge(
        ctx: Context,
        customer_id: str,
        amount_cents: int = Field(ge=1, le=100_000),
        description: str = Field(min_length=1),
        currency: str = "usd",
    ) -> CallToolResult:
        args = {
            "customer_id": customer_id,
            "amount_cents": amount_cents,
            "currency": currency,
            "description": description,
        }
        return await run("payments_charge", args, ctx)

    @tool(
        "mulligan_status", "This session's actions so far and what is waiting for approval.", read
    )
    async def mulligan_status(ctx: Context) -> CallToolResult:
        return await run("mulligan_status", {}, ctx)

    for schema in gateway.upstream.values():
        add_passthrough(mcp, schema, run)

    return mcp, verifier


class PassthroughTool(Tool):
    """An upstream tool, listed with the upstream server's own input schema and forwarded as is.
    The SDK's decorator derives schemas from Python signatures, which upstream tools lack."""

    handler: Any = Field(default=None, exclude=True)

    async def run(
        self, arguments: dict[str, Any], context: Any, convert_result: bool = False
    ) -> CallToolResult:
        result: CallToolResult = await self.handler(arguments, context)
        return result


def add_passthrough(mcp: MCPServer, schema: UpstreamToolSchema, run: Any) -> None:
    async def placeholder(ctx: Context) -> CallToolResult:  # pragma: no cover (never called)
        raise NotImplementedError

    async def handler(arguments: dict[str, Any], ctx: Context) -> CallToolResult:
        result: CallToolResult = await run(schema.exposed_name, dict(arguments or {}), ctx)
        return result

    description = f"[via {schema.server}; held unless read-only] {schema.description or ''}"
    base = Tool.from_function(
        placeholder, name=schema.exposed_name, description=description.strip()
    )
    fields = {k: getattr(base, k) for k in Tool.model_fields}
    fields["parameters"] = schema.input_schema or {"type": "object", "properties": {}}
    tool = PassthroughTool.model_construct(**fields, handler=handler)
    mcp._tool_manager._tools[schema.exposed_name] = tool


# --- ASGI wrappers ----------------------------------------------------------------------------


def allowed_host(host: str, public_host: str) -> bool:
    name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    return (
        name in LOCAL_HOSTS
        or (bool(public_host) and name == public_host)
        or name.endswith(TUNNEL_SUFFIX)
    )


class HostCheck:
    """DNS-rebinding guard: only localhost, the configured public host and quick-tunnel hosts.
    Done here instead of in the SDK, whose exact-match list would need a restart per tunnel URL."""

    def __init__(self, app: ASGIApp, public_host: str) -> None:
        self.app = app
        self.public_host = public_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            host = dict(scope["headers"]).get(b"host", b"").decode("latin-1")
            if not allowed_host(host, self.public_host):
                await _send_json(send, 421, {"detail": "invalid host"})
                return
        await self.app(scope, receive, send)


class ForceHandshake:
    """An authenticated request on a non-handshake protocol version (the sessionless 2026-07-28
    revision) gets a JSON-RPC error, so `auto` clients fall back to `initialize` and get an
    Mcp-Session-Id. Unauthenticated requests pass through so they still get the SDK's 401."""

    def __init__(self, app: ASGIApp, verifier: TokenVerifier) -> None:
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] == "POST":
            headers = dict(scope["headers"])
            version = headers.get(b"mcp-protocol-version", b"").decode("latin-1")
            scheme, _, token = headers.get(b"authorization", b"").decode("latin-1").partition(" ")
            if (
                version
                and version not in HANDSHAKE_PROTOCOL_VERSIONS
                and scheme.lower() == "bearer"
                and await self.verifier.verify_token(token)
            ):
                body = b""
                while True:
                    message = await receive()
                    body += message.get("body", b"")
                    if not message.get("more_body"):
                        break
                try:
                    request_id = json.loads(body).get("id")
                except (ValueError, AttributeError):
                    request_id = None
                await _send_json(
                    send,
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "use the initialize handshake"},
                    },
                )
                return
        await self.app(scope, receive, send)


async def _send_json(send: Send, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_mcp_app(settings: Settings, gateway: Gateway) -> FastAPI:
    mcp, verifier = build_mcp(settings, gateway)
    inner = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False)
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # A mounted sub-app's lifespan never runs, so the host app enters the session manager.
        async with mcp.session_manager.run():
            yield

    # No /docs or /openapi.json: this port is public through the tunnel and serves /mcp only.
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    public_host = urlsplit(settings.mcp_public_url).hostname or ""
    app.mount("/", HostCheck(ForceHandshake(inner, verifier), public_host))
    return app
