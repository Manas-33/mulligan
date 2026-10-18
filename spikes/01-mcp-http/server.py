"""Spikes 0.1 + 0.2: MCP (Streamable HTTP) and REST as two uvicorn servers in one process.

MCP  -> 127.0.0.1:$MULLIGAN_MCP_PORT  (/mcp only; the only port the tunnel exposes)
REST -> 127.0.0.1:$MULLIGAN_API_PORT  (/healthz, /api/*)

Both servers share module state (CALLS), the way the gateway's action log and event bus will.
"""

import asyncio
import hashlib
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import AnyHttpUrl

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

log = logging.getLogger("spike")

BIND = "127.0.0.1"
API_PORT = int(os.environ.get("MULLIGAN_API_PORT", "8000"))
MCP_PORT = int(os.environ.get("MULLIGAN_MCP_PORT", "8001"))
# Bare hostname of the public tunnel, e.g. "abc-def.trycloudflare.com". Empty means local only.
PUBLIC_HOST = os.environ.get("MULLIGAN_MCP_PUBLIC_HOST", "")
# Reject 2026-07-28 requests so clients fall back to the initialize handshake and get an Mcp-Session-Id.
FORCE_HANDSHAKE = os.environ.get("MULLIGAN_FORCE_HANDSHAKE", "1") == "1"

AGENT_TOKENS = {os.environ["SPIKE_AGENT_TOKEN"]: "spike-agent"}
API_TOKEN = os.environ["SPIKE_API_TOKEN"]

CALLS: list[dict] = []  # shared between the two servers


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:12]


# --- MCP server -------------------------------------------------------------


class StaticTokenVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        for known, agent_id in AGENT_TOKENS.items():
            if secrets.compare_digest(token, known):
                return AccessToken(token=token, client_id=agent_id, scopes=[])
        return None


# resource_server_url=None: no /.well-known/oauth-protected-resource route and no resource_metadata
# in the 401, so clients aren't sent off on an OAuth flow. issuer_url is required but then unused.
verifier = StaticTokenVerifier()
mcp = MCPServer(
    "mulligan-spike",
    token_verifier=verifier,
    auth=AuthSettings(issuer_url=AnyHttpUrl(f"http://{BIND}:{MCP_PORT}"), resource_server_url=None),
)


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Report which agent, MCP session and protocol version the gateway sees for this call."""
    token = get_access_token()
    headers = ctx.headers or {}
    seen = {
        "agent": token.client_id if token else None,
        "token_sha256_12": fingerprint(token.token) if token else None,
        "mcp_session_id": headers.get("mcp-session-id"),
        "protocol_version": ctx.protocol_version,
        "host": headers.get("host"),
        "user_agent": headers.get("user-agent"),
    }
    CALLS.append(seen)
    log.info("tools/call whoami %s", seen)
    return seen


hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
if PUBLIC_HOST:
    hosts += [PUBLIC_HOST, f"{PUBLIC_HOST}:*"]
mcp_starlette = mcp.streamable_http_app(transport_security=TransportSecuritySettings(allowed_hosts=hosts))


class ForceHandshake:
    """ASGI wrapper: an authenticated request at a non-handshake protocol version gets a JSON-RPC error.

    The SDK routes on the MCP-Protocol-Version header, so the `server/discover` probe carries it. A client
    in auto mode treats any error on the probe as "legacy server" and runs `initialize` instead.
    Unauthenticated requests pass through untouched so they still get the SDK's 401.
    """

    def __init__(self, app, verifier: TokenVerifier):
        self.app, self.verifier = app, verifier

    async def __call__(self, scope, receive, send):
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
                log.info("rejecting %s request so the client falls back to initialize", version)
                payload = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": "use the initialize handshake (2025-11-25)"},
                }
                await send({"type": "http.response.start", "status": 400,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": json.dumps(payload).encode()})
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def mcp_lifespan(app: FastAPI):
    # A mounted sub-app's lifespan never runs, so the host app enters the session manager.
    async with mcp.session_manager.run():
        yield


# No /docs or /openapi.json: this port is public through the tunnel and serves /mcp only.
mcp_app = FastAPI(lifespan=mcp_lifespan, docs_url=None, redoc_url=None, openapi_url=None)
mcp_app.mount("/", ForceHandshake(mcp_starlette, verifier) if FORCE_HANDSHAKE else mcp_starlette)


# --- REST server ------------------------------------------------------------


def require_api_token(request: Request) -> None:
    if request.url.path == "/healthz":
        return
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(value, API_TOKEN):
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})


# App-level dependencies don't cover FastAPI's own /docs, /redoc and /openapi.json, so turn those off.
api_app = FastAPI(dependencies=[Depends(require_api_token)], docs_url=None, redoc_url=None, openapi_url=None)


@api_app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@api_app.get("/api/calls")
def calls() -> list[dict]:
    return CALLS


# --- one process, two servers -----------------------------------------------


async def main() -> None:
    servers = [
        uvicorn.Server(uvicorn.Config(api_app, host=BIND, port=API_PORT)),
        uvicorn.Server(uvicorn.Config(mcp_app, host=BIND, port=MCP_PORT)),
    ]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
