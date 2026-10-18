"""The email adapter: `email_send`, held until someone approves, sent through SMTP (Mailpit).

`email_send` is always held. Before it runs, a rewind just cancels it (the hold manager's
`cancel`, i.e. the outbox entry is dropped). Once approved it sends here, one message per
recipient so customers never see each other's addresses. A sent email can't be recalled, so
rewinding it sends a short correction to the same people (`email.send_correction`,
``compensated``), or does nothing (``skipped``) when EMAIL_SEND_CORRECTIONS=0.

Mailpit (docker compose service `mailpit`) catches everything: SMTP on :1025, inbox UI on :8025.
"""

from __future__ import annotations

import asyncio
import smtplib
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate
from typing import ClassVar, Protocol

from mulligan.holds.notify import holds_env
from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult
from mulligan.models import Action, Inverse, RewindStep, StepResult, System

CORRECTION = "email.send_correction"


@dataclass(frozen=True)
class Outgoing:
    to: str
    subject: str
    body: str
    cc: tuple[str, ...] = ()


class Mailer(Protocol):
    def send(self, sender: str, messages: list[Outgoing]) -> list[str]:
        """Send each message; return their Message-IDs."""
        ...


@dataclass(frozen=True)
class SmtpMailer:
    host: str
    port: int
    timeout_s: float = 10.0

    def send(self, sender: str, messages: list[Outgoing]) -> list[str]:
        ids: list[str] = []
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout_s) as smtp:
            for m in messages:
                msg = EmailMessage()
                msg["From"] = sender
                msg["To"] = m.to
                if m.cc:
                    msg["Cc"] = ", ".join(m.cc)
                msg["Subject"] = m.subject
                msg["Date"] = formatdate(localtime=False)
                msg["Message-ID"] = f"<{uuid.uuid4().hex}@mulligan.local>"
                msg.set_content(m.body)
                smtp.send_message(msg)
                ids.append(str(msg["Message-ID"]))
        return ids


def smtp_from_env() -> SmtpMailer:
    env = holds_env()
    return SmtpMailer(
        host=env.get("EMAIL_SMTP_HOST", "").strip() or "127.0.0.1",
        port=int(env.get("EMAIL_SMTP_PORT", "").strip() or 1025),
    )


def _step(action: Action, result: StepResult, detail: str) -> RewindStep:
    return RewindStep(
        action_id=action.id,
        system=action.system,
        tool=action.tool,
        result=result,
        kept_fields=[],
        changes=[],
        changes_truncated=False,
        detail=detail,
    )


def correction_body(subject: str) -> str:
    return (
        f'Please disregard our earlier email "{subject}". It was sent in error and has been '
        "withdrawn. No action is needed on your part. We're sorry for the confusion."
    )


class EmailAdapter(Adapter):
    system: ClassVar[System] = System.email
    tools: ClassVar[frozenset[str]] = frozenset({"email_send"})

    def __init__(
        self,
        mailer: Mailer | None = None,
        sender: str | None = None,
        send_corrections: bool | None = None,
    ) -> None:
        env = holds_env()
        self.mailer = mailer or smtp_from_env()
        self.sender = sender or env.get("EMAIL_FROM", "").strip() or "billing@acme-widgets.test"
        self.send_corrections = (
            send_corrections
            if send_corrections is not None
            else env.get("EMAIL_SEND_CORRECTIONS", "1").strip() != "0"
        )

    async def capture_before(self, call: ToolCall) -> Capture:
        return Capture(ref=None)

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        to = [str(a) for a in call.args["to"]]
        cc = tuple(str(a) for a in call.args.get("cc") or [])
        subject, body = str(call.args["subject"]), str(call.args["body"])
        messages = [Outgoing(to=a, subject=subject, body=body, cc=cc) for a in to]
        ids = await asyncio.to_thread(self.mailer.send, self.sender, messages)
        before.state.update(recipients=to, cc=list(cc), subject=subject, message_ids=ids)
        n = len(to)
        return ToolResult(
            structured_content={"sent": n, "subject": subject},
            summary=f'Emailed {n} recipient{"s" if n != 1 else ""}: "{subject}"'[:300],
        )

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        return Capture(ref=f"smtp:{len(before.state['message_ids'])} sent")

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        return Inverse(kind=CORRECTION, payload=dict(before.state))

    async def plan_revert(self, action: Action) -> RewindStep:
        if action.inverse is None:
            return _step(action, StepResult.skipped, "Nothing was sent.")
        n = len(action.inverse.payload["recipients"])
        if not self.send_corrections:
            return _step(action, StepResult.skipped, f"{n} emails already sent; can't be recalled.")
        return _step(action, StepResult.pending, f"Send a correction to the {n} recipients.")

    async def revert(self, action: Action) -> RewindStep:
        if action.inverse is None:
            return _step(action, StepResult.skipped, "Nothing was sent.")
        p = action.inverse.payload
        n = len(p["recipients"])
        if not self.send_corrections:
            return _step(action, StepResult.skipped, f"{n} emails already sent; can't be recalled.")
        subject = f"Correction: {p['subject']}"
        messages = [
            Outgoing(to=a, subject=subject, body=correction_body(p["subject"]), cc=tuple(p["cc"]))
            for a in p["recipients"]
        ]
        try:
            await asyncio.to_thread(self.mailer.send, self.sender, messages)
        except Exception as exc:  # noqa: BLE001 (revert reports failures, never raises)
            return _step(action, StepResult.failed, f"Correction not sent: {exc}"[:300])
        return _step(action, StepResult.compensated, f"Sent a correction to {n} recipients.")

    async def checksum(self) -> str | None:
        return None  # a held channel: nothing to verify by hash
