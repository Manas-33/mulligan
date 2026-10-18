"""The slice of Mulligan's REST API the Guardian uses, with the service token."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

POLL_SECONDS = 0.5
REWIND_TIMEOUT_SECONDS = 120.0


class MulliganError(RuntimeError):
    pass


class MulliganAPI:
    def __init__(self, base_url: str, service_token: str, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self.client.headers["X-Mulligan-Service-Token"] = service_token

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MulliganError(f"{method} {path}: {type(exc).__name__} reaching Mulligan") from exc
        if response.status_code >= 400:
            detail: Any = response.text
            try:
                detail = response.json().get("detail", detail)
            except ValueError:
                pass
            raise MulliganError(f"{method} {path} -> {response.status_code}: {detail}")
        return response.json()

    async def action(self, action_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._request("GET", f"/api/actions/{action_id}")
        return result

    async def session_actions(self, session_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self._request(
            "GET", "/api/actions", params={"session_id": session_id, "limit": 1000}
        )
        return result

    async def decide(
        self, action_id: str, decision: str, code: str, decided_by: str
    ) -> dict[str, Any]:
        body = {"decision": decision, "code": code, "channel": "asi_one", "decided_by": decided_by}
        result: dict[str, Any] = await self._request(
            "POST", f"/api/approvals/{action_id}/decision", json=body
        )
        return result

    async def rewind(self, session_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        """Start a rewind of the session and wait for it to finish (or return the plan)."""
        job: dict[str, Any] = await self._request(
            "POST",
            "/api/rewind",
            json={
                "scope": {"session_id": session_id},
                "dry_run": dry_run,
                "requested_by": "asi_one",
            },
        )
        waited = 0.0
        while job.get("status") == "running":
            if waited >= REWIND_TIMEOUT_SECONDS:
                raise MulliganError(f"rewind {job['id']} still running after {waited:.0f}s")
            await asyncio.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
            job = await self._request("GET", f"/api/rewind/{job['id']}")
        return job

    async def close(self) -> None:
        await self.client.aclose()
