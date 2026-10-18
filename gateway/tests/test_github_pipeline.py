"""GitHub calls through the gateway and a rewind through the orchestrator, against the in-memory
GitHub and a real mulligan-db (needs `make db`)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mulligan.adapters.github.adapter import GitHubAdapter
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    ActionStatus,
    Agent,
    AgentKind,
    RewindJob,
    RewindJobStatus,
    RewindRequest,
    RewindScope,
    Session,
    SessionMode,
    StepResult,
)
from mulligan.orchestrator.rewind import MAY_HAVE_LANDED
from mulligan.services import Services, assemble
from mulligan.settings import Settings
from tests.github_fake import FakeGitHub

pytestmark = pytest.mark.integration

AGENT = Agent(id="rogue", kind=AgentKind.rogue, display_name="Cleanup agent")


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    for n in range(1, 6):
        fake.add_issue(f"Stale issue {n}", labels=["stale"])
    fake.put_file("docs/pricing.md", "Widget: $49\n")
    return fake


@pytest.fixture
async def svc(log_pool: Any, gh: FakeGitHub) -> AsyncIterator[Services]:
    adapter = GitHubAdapter(gh.client(), "main")
    try:
        yield assemble(Settings(), LogStore(log_pool), EventBus(log_pool), [adapter])
    finally:
        await adapter.close()


async def rewind(svc: Services, session: Session) -> RewindJob:
    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None
    return done


async def test_cleanup_session_rewinds_and_verifies(svc: Services, gh: FakeGitHub) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    listed = await call(session, "github_list_issues", {"labels": ["stale"]})
    numbers = [i["number"] for i in listed.structured_content["issues"]]
    assert len(numbers) == 5
    for n in numbers[:3]:
        out = await call(session, "github_close_issue", {"number": n, "comment": "Stale, closing."})
        assert out.structured_content["status"] == "executed"
    await call(
        session,
        "github_update_file",
        {"path": "docs/pricing.md", "content": "Everything is free\n", "message": "Simplify"},
    )
    closed = [n for n, i in gh.issues.items() if i["state"] == "closed"]
    gh.issues[closed[0]]["title"] = "Teammate renamed this"
    agent_comment = next(c for c in gh.comments.values() if c["_n"] == closed[1])
    agent_comment["body"] = "Teammate: keep this one open, it's not stale."

    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed, job
    results: dict[str, list[StepResult]] = defaultdict(list)
    for s in job.steps:
        results[s.tool].append(s.result)
    assert results["github_update_file"] == [StepResult.restored]
    assert sorted(results["github_close_issue"]) == sorted(
        [StepResult.restored, StepResult.restored, StepResult.kept_human_edit]
    )
    kept = next(s for s in job.steps if s.result is StepResult.kept_human_edit)
    assert kept.kept_fields == [f"github:comments/{agent_comment['id']}"]
    assert job.verification["github"].ok is True
    assert all(i["state"] == "open" for i in gh.issues.values())
    assert gh.issues[closed[0]]["title"] == "Teammate renamed this"
    assert list(gh.comments) == [agent_comment["id"]]
    assert gh.files["docs/pricing.md"] == b"Widget: $49\n"
    action = await svc.store.get(kept.action_id)
    assert action is not None and action.status is ActionStatus.conflict


async def test_comment_that_landed_without_an_answer_is_rewound(
    svc: Services, gh: FakeGitHub
) -> None:
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    gh.lose.append("POST issues/1/comments")
    out = await svc.gateway.call_tool(
        session, "github_comment", {"number": 1, "body": "Pinging the owner."}
    )
    assert out.is_error and "may have landed" in out.summary
    action = await svc.store.get(out.structured_content["action_id"])
    assert action is not None and action.status is ActionStatus.failed
    assert action.inverse is not None
    (cid,) = gh.comments

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    (step,) = plan.steps
    assert step.detail == f"{MAY_HAVE_LANDED}Delete comment {cid}"

    job = await rewind(svc, session)
    assert job.status is RewindJobStatus.completed, job
    assert [s.result for s in job.steps] == [StepResult.restored]
    assert job.verification["github"].ok is True
    assert not gh.comments
    action = await svc.store.get(action.id)
    assert action is not None and action.status is ActionStatus.reverted
