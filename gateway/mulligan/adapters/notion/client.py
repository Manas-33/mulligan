"""A small async client for the Notion API (version 2026-03-11): paced to ~3 requests/s, with
retries for rate limits, overloads and read timeouts."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

import httpx

API_URL = "https://api.notion.com/v1"
NOTION_VERSION = "2026-03-11"
MIN_INTERVAL_S = 0.34  # 180 requests/min per integration
ATTEMPTS = 5
# A 429 or 529 means the request was refused, so even a non-idempotent call can be sent again.
REFUSED = {429, 529}
# Gateway errors may have run the request: only retried when sending it twice is harmless.
TRANSIENT = {500, 502, 503, 504}


class NotionError(Exception):
    """An error response from the Notion API."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"Notion {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code == "object_not_found"


def normalize_id(value: str) -> str:
    """A page or block id, or a Notion URL ending in one, as the dashed UUID the API returns."""
    runs = re.findall(r"[0-9a-fA-F]{32}", value.replace("-", ""))
    if not runs:
        raise ValueError(f"not a Notion id: {value!r}")
    return str(uuid.UUID(runs[-1].lower()))


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval_s: float = MIN_INTERVAL_S,
        timeout_s: float = 30.0,
    ) -> None:
        self.http = httpx.AsyncClient(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": NOTION_VERSION,
                "Content-Type": "application/json",
            },
            timeout=timeout_s,
            transport=transport,
        )
        self.min_interval_s = min_interval_s
        self._lock = asyncio.Lock()
        self._next_at = 0.0
        self.requests = 0

    async def close(self) -> None:
        await self.http.aclose()

    async def _pace(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            self._next_at = max(now, self._next_at) + self.min_interval_s
        if wait > 0:
            await asyncio.sleep(wait)

    async def request(
        self, method: str, path: str, body: dict[str, Any] | None = None, *, idempotent: bool = True
    ) -> dict[str, Any]:
        delay = 1.0
        for attempt in range(1, ATTEMPTS + 1):
            await self._pace()
            self.requests += 1
            try:
                r = await self.http.request(method, path, json=body)
            except (httpx.TimeoutException, httpx.TransportError):
                if not idempotent or attempt == ATTEMPTS:
                    raise
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code < 400:
                data: dict[str, Any] = r.json()
                return data
            retry = r.status_code in REFUSED or (idempotent and r.status_code in TRANSIENT)
            if retry and attempt < ATTEMPTS:
                after = r.headers.get("retry-after", "")
                await asyncio.sleep(float(after) if after.replace(".", "", 1).isdigit() else delay)
                delay *= 2
                continue
            try:
                err = r.json()
            except ValueError:
                err = {}
            raise NotionError(
                r.status_code, str(err.get("code", "error")), str(err.get("message", r.text[:200]))
            )
        raise AssertionError("unreachable")

    # --- endpoints ----------------------------------------------------------------------------

    async def get_page(self, page_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/pages/{page_id}")

    async def update_page(self, page_id: str, **body: Any) -> dict[str, Any]:
        return await self.request("PATCH", f"/pages/{page_id}", body)

    async def get_block(self, block_id: str) -> dict[str, Any]:
        return await self.request("GET", f"/blocks/{block_id}")

    async def update_block(self, block_id: str, **body: Any) -> dict[str, Any]:
        return await self.request("PATCH", f"/blocks/{block_id}", body)

    async def delete_block(self, block_id: str) -> dict[str, Any]:
        return await self.request("DELETE", f"/blocks/{block_id}")

    async def children(self, block_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = f"?page_size=100&start_cursor={cursor}" if cursor else "?page_size=100"
            r = await self.request("GET", f"/blocks/{block_id}/children{query}")
            out += r["results"]
            if not r.get("has_more"):
                return out
            cursor = r["next_cursor"]

    async def append(
        self, parent_id: str, children: list[dict[str, Any]], after: str | None = None
    ) -> list[dict[str, Any]]:
        """Append blocks and return the new ones. With a position, Notion's response also lists
        every later sibling, so only the first len(children) results are new."""
        body: dict[str, Any] = {"children": children}
        if after:
            body["position"] = {"type": "after_block", "after_block": {"id": after}}
        r = await self.request("PATCH", f"/blocks/{parent_id}/children", body, idempotent=False)
        results: list[dict[str, Any]] = r["results"]
        return results[: len(children)]
