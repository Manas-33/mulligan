"""A rewind runs in a background task after the request returns, so it needs its own transaction."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

from mulligan.__main__ import SkipHealthChecks
from mulligan.models import RewindJob, RewindJobStatus, RewindScope
from mulligan.orchestrator.rewind import Rewinder

SES = "ses_01M561Q6R04CJZXHXRCFFNM7C4"


class CaptureTransport(Transport):
    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.envelopes: list[Envelope] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        self.envelopes.append(envelope)

    def transactions(self) -> list[dict[str, Any]]:
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "transaction" and item.payload.json
        ]


@pytest.fixture
def transport() -> Iterator[CaptureTransport]:
    capture = CaptureTransport()
    sentry_sdk.init(
        dsn="https://key@o0.ingest.sentry.io/0", traces_sample_rate=1.0, transport=capture
    )
    yield capture
    sentry_sdk.init()  # back to a disabled client


def job(status: RewindJobStatus = RewindJobStatus.running) -> RewindJob:
    return RewindJob.model_validate(
        {
            "id": "rwd_01M561RFRGVASABYV8XG40DDX0",
            "scope": {"session_id": SES},
            "dry_run": False,
            "status": status.value,
            "steps": [],
            "verification": {},
            "requested_by": "dashboard",
            "error": None,
            "created_at": "2026-10-18T00:00:00Z",
            "started_at": "2026-10-18T00:00:00Z",
            "completed_at": None,
        }
    )


def rewinder(final: RewindJobStatus) -> Rewinder:
    gateway = SimpleNamespace(store=None, bus=None, hold_manager=None, by_system={})
    r = Rewinder(gateway)  # type: ignore[arg-type]

    async def run_job(j: RewindJob, actions: list[Any]) -> RewindJob:
        for tool in ("pg_delete_rows", "github_close_issue"):
            with sentry_sdk.start_span(op="mulligan.rewind.step", name=tool):
                pass
        return j.model_copy(update={"status": final})

    r._run_job = run_job  # type: ignore[method-assign]
    return r


@pytest.mark.parametrize(
    ("final", "status"),
    [(RewindJobStatus.completed, "ok"), (RewindJobStatus.partial, "internal_error")],
)
async def test_rewind_runs_in_its_own_transaction_with_its_steps(
    transport: CaptureTransport, final: RewindJobStatus, status: str
) -> None:
    await rewinder(final)._run(job(), [])
    sentry_sdk.flush()
    [txn] = transport.transactions()
    assert txn["contexts"]["trace"]["op"] == "mulligan.rewind"
    assert txn["transaction"] == "rwd_01M561RFRGVASABYV8XG40DDX0"
    assert txn["contexts"]["trace"]["status"] == status
    assert txn["tags"]["mulligan.session_id"] == SES
    assert txn["tags"]["mulligan.rewind_status"] == final.value
    steps = [s for s in txn["spans"] if s["op"] == "mulligan.rewind.step"]
    assert [s["description"] for s in steps] == ["pg_delete_rows", "github_close_issue"]
    assert {s["parent_span_id"] for s in steps} == {txn["contexts"]["trace"]["span_id"]}


def test_scope_is_recorded() -> None:
    assert RewindScope(session_id=SES).model_dump(mode="json", exclude_none=True) == {
        "session_id": SES
    }


def test_healthchecks_stay_out_of_the_logs() -> None:
    def access(path: str) -> logging.LogRecord:
        # How uvicorn logs a request (uvicorn.protocols.http.h11_impl).
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("172.18.0.1:5000", "GET", path, "1.1", 200),
            None,
        )

    skip = SkipHealthChecks()
    assert not skip.filter(access("/healthz"))
    assert skip.filter(access("/api/actions?limit=5"))
    assert skip.filter(logging.LogRecord("uvicorn.error", logging.INFO, "", 1, "Started", (), None))
