"""Gateway -> Photon service calls (contracts/photon.openapi.yaml).

Notifying is best effort: a held action is decided from iMessage, the dashboard or ASI:One, so a
failed text never fails the hold. It is logged (Sentry picks it up) and the action stays held.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import StrEnum

import httpx

from mulligan.models import Action
from mulligan.settings import REPO_ROOT, load_dotenv

log = logging.getLogger("mulligan.holds")

SUMMARY_MAX = 300


def holds_env() -> dict[str, str]:
    """The repo's .env overlaid by the process environment, the same precedence as Settings."""
    return {**load_dotenv(REPO_ROOT / ".env"), **os.environ}


class NotifyOutcome(StrEnum):
    queued = "queued"
    not_reachable = "not_reachable"  # 409: the approver hasn't texted the shared line yet
    disabled = "disabled"  # no Photon service or approver configured
    failed = "failed"


def approval_summary(action: Action) -> str:
    """One line for the approver's text: 'Claude Code wants to run email_send: Emailed 412…'."""
    what = action.summary or ", ".join(
        f"{k}={v}" for k, v in list(action.args_redacted.items())[:3]
    )
    text = f"{action.agent.display_name} wants to run {action.tool}"
    text = f"{text}: {what}" if what else text
    return text if len(text) <= SUMMARY_MAX else text[: SUMMARY_MAX - 1] + "…"


@dataclass(frozen=True)
class PhotonNotifier:
    base_url: str
    service_token: str
    approver: str
    """The approver's iMessage handle: an E.164 phone or an Apple ID email (APPROVER_HANDLE)."""
    timeout_s: float = 5.0
    transport: httpx.AsyncBaseTransport | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> PhotonNotifier:
        env = holds_env() if env is None else env
        return cls(
            base_url=env.get("PHOTON_SERVICE_URL", "").strip().rstrip("/"),
            service_token=env.get("MULLIGAN_SERVICE_TOKEN", "").strip(),
            approver=env.get("APPROVER_HANDLE", "").strip()
            or env.get("APPROVER_PHONE", "").strip(),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.service_token and self.approver)

    async def _post(self, path: str, body: dict[str, str]) -> NotifyOutcome:
        if not self.enabled:
            return NotifyOutcome.disabled
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url, timeout=self.timeout_s, transport=self.transport
            ) as client:
                res = await client.post(
                    path, json=body, headers={"X-Mulligan-Service-Token": self.service_token}
                )
        except httpx.HTTPError:
            log.exception("Photon service unreachable (%s)", path)
            return NotifyOutcome.failed
        if res.status_code == 202:
            return NotifyOutcome.queued
        if res.status_code == 409:
            log.warning(
                "approver not reachable on iMessage yet; decide from the dashboard (%s)", path
            )
            return NotifyOutcome.not_reachable
        log.error("Photon service answered %s on %s: %s", res.status_code, path, res.text[:200])
        return NotifyOutcome.failed

    async def notify_approval(self, action: Action) -> NotifyOutcome:
        assert action.approval is not None
        return await self._post(
            "/notify/approval",
            {
                "action_id": action.id,
                "code": action.approval.code,
                "summary": approval_summary(action),
                "to": self.approver,
            },
        )

    async def notify_summary(self, rewind_job_id: str, text: str) -> NotifyOutcome:
        return await self._post(
            "/notify/summary", {"rewind_job_id": rewind_job_id, "text": text, "to": self.approver}
        )
