"""Token auth for the REST API. Every request needs a token except GET /healthz.

This is ASGI middleware rather than a FastAPI dependency so it also covers unknown paths, the
SSE stream and anything mounted later. CORS sits outside it, so preflights pass and a 401 still
carries CORS headers the browser can read.
"""

from __future__ import annotations

import hmac
import json
from typing import Literal

from fastapi import HTTPException, Request, status
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

Caller = Literal["dashboard", "service"]

HEALTH_PATHS = frozenset({"/healthz"})


def _matches(given: str | None, expected: str) -> bool:
    return bool(given) and bool(expected) and hmac.compare_digest(given or "", expected)


def caller_for(headers: Headers, dashboard_token: str, service_token: str) -> Caller | None:
    """Which kind of caller the request's token belongs to, or None."""
    scheme, _, bearer = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and _matches(bearer.strip(), dashboard_token):
        return "dashboard"
    if _matches(headers.get("x-mulligan-service-token"), service_token):
        return "service"
    return None


class TokenAuthMiddleware:
    def __init__(self, app: ASGIApp, dashboard_token: str, service_token: str) -> None:
        self.app = app
        self.dashboard_token = dashboard_token
        self.service_token = service_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in HEALTH_PATHS:
            await self.app(scope, receive, send)
            return
        caller = caller_for(Headers(scope=scope), self.dashboard_token, self.service_token)
        if caller is None:
            body = json.dumps({"detail": "missing or invalid token"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        scope.setdefault("state", {})["caller"] = caller
        await self.app(scope, receive, send)


async def require_dashboard(request: Request) -> None:
    """Route dependency for dashboard-only routes such as /api/demo/reset."""
    if getattr(request.state, "caller", None) != "dashboard":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="dashboard token required")
