"""Stream B's email adapter, with a fake mailer and a real local SMTP sink."""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import pytest

from mulligan.holds.email import CORRECTION, EmailAdapter, Outgoing, SmtpMailer
from mulligan.interfaces import ToolCall
from mulligan.models import ActionStatus, StepResult, System
from tests.test_holds import AGENT, make_action


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Outgoing]] = []
        self.fail = False

    def send(self, sender: str, messages: list[Outgoing]) -> list[str]:
        if self.fail:
            raise ConnectionRefusedError("smtp down")
        self.sent += [(sender, m) for m in messages]
        return [f"<{i}@test>" for i in range(len(messages))]


def email_call() -> ToolCall:
    return ToolCall(
        action_id="act_01M561R2302EME3JAF7RBZWM01",
        session_id="ses_01M561Q6R04CJZXHXRCFFNM7C4",
        agent=AGENT,
        tool="email_send",
        system=System.email,
        args={
            "to": ["a@acme-widgets.test", "b@acme-widgets.test"],
            "subject": "Price update",
            "body": "Prices change next month.",
        },
    )


async def send(adapter: EmailAdapter) -> Any:
    call = email_call()
    before = await adapter.capture_before(call)
    result = await adapter.execute(call, before)
    after = await adapter.capture_after(call, before, result)
    return result, await adapter.build_inverse(call, before, after, result)


def sent_action(inverse: Any) -> Any:
    return make_action().model_copy(update={"status": ActionStatus.executed, "inverse": inverse})


async def test_sends_one_message_per_recipient() -> None:
    mailer = FakeMailer()
    result, inverse = await send(EmailAdapter(mailer, sender="billing@acme-widgets.test"))
    assert result.summary == 'Emailed 2 recipients: "Price update"'
    assert [m.to for _, m in mailer.sent] == ["a@acme-widgets.test", "b@acme-widgets.test"]
    assert {s for s, _ in mailer.sent} == {"billing@acme-widgets.test"}
    assert inverse.kind == CORRECTION and inverse.payload["recipients"] == [
        "a@acme-widgets.test",
        "b@acme-widgets.test",
    ]


async def test_rewind_sends_a_correction() -> None:
    mailer = FakeMailer()
    adapter = EmailAdapter(mailer, sender="x@test", send_corrections=True)
    _, inverse = await send(adapter)
    action = sent_action(inverse)
    assert (await adapter.plan_revert(action)).detail == "Send a correction to the 2 recipients."
    step = await adapter.revert(action)
    assert (step.result, step.detail) == (
        StepResult.compensated,
        "Sent a correction to 2 recipients.",
    )
    corrections = [m for _, m in mailer.sent if m.subject.startswith("Correction:")]
    assert len(corrections) == 2 and "disregard" in corrections[0].body


async def test_corrections_off_means_skipped_and_errors_are_reported() -> None:
    mailer = FakeMailer()
    _, inverse = await send(EmailAdapter(mailer, sender="x@test"))
    off = EmailAdapter(mailer, sender="x@test", send_corrections=False)
    step = await off.revert(sent_action(inverse))
    assert step.result is StepResult.skipped and "can't be recalled" in (step.detail or "")

    mailer.fail = True
    step = await EmailAdapter(mailer, sender="x@test", send_corrections=True).revert(
        sent_action(inverse)
    )
    assert step.result is StepResult.failed and "smtp down" in (step.detail or "")


async def test_real_smtp_round_trip() -> None:
    """SmtpMailer against a tiny in-process SMTP sink (no Mailpit needed)."""
    received: list[str] = []
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def sink() -> None:
        conn, _ = server.accept()
        f = conn.makefile("rwb")

        def say(line: str) -> None:
            f.write((line + "\r\n").encode())
            f.flush()

        say("220 sink")
        data = False
        for raw in f:
            line = raw.decode().rstrip("\r\n")
            if data:
                if line == ".":
                    data = False
                    say("250 queued")
                else:
                    received.append(line)
                continue
            cmd = line.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                say("250 sink")
            elif cmd == "DATA":
                data = True
                say("354 go")
            elif cmd == "QUIT":
                say("221 bye")
                break
            else:
                say("250 ok")
        conn.close()

    t = threading.Thread(target=sink, daemon=True)
    t.start()
    ids = await asyncio.to_thread(
        SmtpMailer("127.0.0.1", port).send,
        "billing@acme-widgets.test",
        [Outgoing(to="a@acme-widgets.test", subject="Hi", body="Body")],
    )
    t.join(timeout=5)
    server.close()
    assert len(ids) == 1
    assert "Subject: Hi" in received and "To: a@acme-widgets.test" in received


async def test_cc_gets_one_copy() -> None:
    mailer = FakeMailer()
    adapter = EmailAdapter(mailer, sender="billing@acme-widgets.test")
    call = email_call()
    call = call.model_copy(update={"args": {**call.args, "cc": ["boss@acme-widgets.test"]}})
    await adapter.execute(call, await adapter.capture_before(call))
    assert [m.cc for _, m in mailer.sent] == [("boss@acme-widgets.test",), ()]


async def test_a_send_that_stops_partway_keeps_a_correction_for_who_got_it() -> None:
    from mulligan.holds.email import PartialSend
    from mulligan.interfaces import WriteUncertain

    class Partway(FakeMailer):
        def send(self, sender: str, messages: list[Outgoing]) -> list[str]:
            raise PartialSend(["a@acme-widgets.test"], ["<0@test>"], OSError("connection reset"))

    adapter = EmailAdapter(Partway(), sender="billing@acme-widgets.test")
    call = email_call()
    with pytest.raises(WriteUncertain) as raised:
        await adapter.execute(call, await adapter.capture_before(call))
    assert "1 of 2" in str(raised.value)
    assert raised.value.inverse.kind == CORRECTION
    assert raised.value.inverse.payload["recipients"] == ["a@acme-widgets.test"]
