"""A small async client for one GitHub repository: the API version header, serial and paced
writes, rate-limit waits and Link-header pagination (see docs/SDK_NOTES.md)."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import quote

import httpx

API_VERSION = "2026-03-10"
BASE_URL = "https://api.github.com"
WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
MAX_ATTEMPTS = 4


class GitHubError(Exception):
    """A GitHub response the caller didn't expect. Never carries the token."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub {status}: {message}")
        self.status = status
        self.message = message


def _message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "error"
    if isinstance(body, dict):
        text = str(body.get("message") or response.reason_phrase or "error")
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            details = [e.get("message") or e.get("code") for e in errors if isinstance(e, dict)]
            text += " (" + "; ".join(str(d) for d in details if d) + ")"
        return text[:300]
    return response.reason_phrase or "error"


class GitHubClient:
    """Requests against ``/repos/<org>/<repo>``. Paths without a leading slash are relative to it.

    Writes go one at a time, at least ``write_interval`` seconds apart, as GitHub asks for bulk
    content creation. A rate-limited request (403 or 429 with ``retry-after`` or no remaining
    quota) waits as told, up to ``max_wait`` seconds, and is retried.
    """

    def __init__(
        self,
        token: str,
        org: str,
        repo: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = BASE_URL,
        write_interval: float = 1.0,
        max_wait: float = 90.0,
    ) -> None:
        self.org = org
        self.repo = repo
        self.repo_path = f"/repos/{quote(org)}/{quote(repo)}"
        self.write_interval = write_interval
        self.max_wait = max_wait
        self.http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "mulligan-gateway",
            },
            timeout=httpx.Timeout(20.0, connect=10.0),
            transport=transport,
        )
        self._write_lock = asyncio.Lock()
        self._last_write = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    def url(self, path: str) -> str:
        if path.startswith(("/", "http://", "https://")):
            return path
        return f"{self.repo_path}/{path}" if path else self.repo_path

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        allow_404: bool = False,
    ) -> httpx.Response | None:
        """The response for a 2xx, ``None`` for a 404 when ``allow_404``; raises otherwise."""
        method = method.upper()
        if method in WRITE_METHODS:
            async with self._write_lock:
                wait = self._last_write + self.write_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                try:
                    return await self._send(method, path, params, json, allow_404)
                finally:
                    self._last_write = time.monotonic()
        return await self._send(method, path, params, json, allow_404)

    async def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json: Any,
        allow_404: bool,
    ) -> httpx.Response | None:
        url = self.url(path)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.http.request(method, url, params=params, json=json)
            except httpx.TransportError as exc:
                # Only reads are safe to resend: a write may have landed before the error.
                if method != "GET" or attempt == MAX_ATTEMPTS:
                    raise GitHubError(0, f"{type(exc).__name__} talking to GitHub") from exc
                await asyncio.sleep(attempt)
                continue
            if response.is_success:
                return response
            if response.status_code == 404 and allow_404:
                return None
            wait = self._rate_limit_wait(response)
            if wait is not None and attempt < MAX_ATTEMPTS and wait <= self.max_wait:
                await asyncio.sleep(wait)
                continue
            if response.status_code >= 500 and method == "GET" and attempt < MAX_ATTEMPTS:
                await asyncio.sleep(attempt)
                continue
            raise GitHubError(response.status_code, _message(response))
        raise AssertionError("unreachable")

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> float | None:
        """Seconds to wait before retrying, or ``None`` if this isn't a rate limit."""
        if response.status_code not in (403, 429):
            return None
        headers = response.headers
        if retry_after := headers.get("retry-after"):
            try:
                return max(float(retry_after), 1.0)
            except ValueError:
                return 60.0
        if headers.get("x-ratelimit-remaining") == "0":
            reset = float(headers.get("x-ratelimit-reset") or 0)
            return max(reset - time.time(), 1.0)
        if "rate limit" in _message(response).lower():
            return 60.0
        return None

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.request("GET", path, params=params)
        assert response is not None
        return response.json()

    async def get_or_none(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self.request("GET", path, params=params, allow_404=True)
        return None if response is None else response.json()

    async def send(self, method: str, path: str, body: Any = None) -> Any:
        response = await self.request(method, path, json=body)
        assert response is not None
        return response.json() if response.content else None

    async def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Every item of a list endpoint, following ``Link: rel="next"``."""
        items: list[Any] = []
        url: str | None = self.url(path)
        query: dict[str, Any] | None = {"per_page": 100, **(params or {})}
        while url:
            response = await self.request("GET", url, params=query)
            assert response is not None
            page = response.json()
            items.extend(page if isinstance(page, list) else [])
            url = response.links.get("next", {}).get("url")
            query = None  # the next link carries the query
        return items


def content_path(path: str) -> str:
    """``contents/<path>`` with each segment percent-encoded."""
    return "contents/" + quote(path, safe="/")
