"""A real rewind on the GitHub test repo (GITHUB_ORG/GITHUB_REPO, seeded by
demo/seed/github_seed.py). Opt in, since it notifies watchers and leaves commits:

  MULLIGAN_LIVE_GITHUB=1 uv run pytest -m live

Needs `make db` for the action log. Reads GITHUB_* from .env, or from the file in MULLIGAN_DOTENV.
Leaves the repository seeded and clean.
"""

from __future__ import annotations

import base64
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from mulligan.adapters.github.adapter import GitHubAdapter, load_seed
from mulligan.adapters.github.client import content_path
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    Agent,
    AgentKind,
    RewindRequest,
    RewindScope,
    SessionMode,
    StepResult,
)
from mulligan.services import Services, assemble
from mulligan.settings import REPO_ROOT, Settings, load_dotenv

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MULLIGAN_LIVE_GITHUB") != "1", reason="set MULLIGAN_LIVE_GITHUB=1"
    ),
]

AGENT = Agent(id="rogue", kind=AgentKind.rogue, display_name="Cleanup agent")


def live_settings() -> Settings:
    dotenv = Path(os.environ.get("MULLIGAN_DOTENV") or REPO_ROOT / ".env")
    settings = Settings.from_env({**load_dotenv(dotenv), **os.environ})
    if not settings.github_token:
        pytest.skip("GITHUB_TOKEN is not set")
    return settings


@pytest.fixture
async def svc(log_pool: Any) -> AsyncIterator[Services]:
    adapter = await GitHubAdapter.open(live_settings())
    seed = load_seed()
    await seed.reset(adapter.client)
    try:
        yield assemble(Settings(), LogStore(log_pool), EventBus(log_pool), [adapter])
    finally:
        await seed.reset(adapter.client)
        assert (await seed.apply(adapter.client, write=False)).actions == []
        await adapter.close()


async def test_live_rewind_keeps_the_teammates_edits(svc: Services) -> None:
    adapter = svc.gateway.by_system[GitHubAdapter.system]
    assert isinstance(adapter, GitHubAdapter)
    gh = adapter.client
    seed = load_seed()
    numbers = (await seed.apply(gh, write=False)).numbers
    a, b = numbers[44], numbers[45]  # two stale seed issues
    pricing = await gh.get(content_path(seed.PRICING_PATH))

    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    for n in (a, b):
        out = await call(
            session,
            "github_close_issue",
            {
                "number": n,
                "comment": "Closing as stale (live test).",
                "state_reason": "not_planned",
            },
        )
        assert out.structured_content["status"] == "executed", out.summary
    out = await call(
        session,
        "github_update_file",
        {
            "path": seed.PRICING_PATH,
            "content": seed.PRICING.replace("$49.00", "$0.00"),
            "message": "Simplify pricing (live test)",
        },
    )
    assert out.structured_content["status"] == "executed", out.summary

    # A teammate, straight on GitHub: renames issue a, and edits the agent's comment on b.
    await gh.send("PATCH", f"issues/{a}", {"title": "Teammate renamed this"})
    comments = await gh.paginate(f"issues/{b}/comments")
    agent_comment = next(c for c in comments if "live test" in c["body"])
    edited = "Teammate: this one isn't stale, keeping my note."
    await gh.send("PATCH", f"issues/comments/{agent_comment['id']}", {"body": edited})

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    assert [s.kept_fields for s in plan.steps] == [
        [],
        [f"github:comments/{agent_comment['id']}"],
        [],
    ]

    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None
    assert [s.result for s in done.steps] == [
        StepResult.restored,
        StepResult.kept_human_edit,
        StepResult.restored,
    ], [s.detail for s in done.steps]
    kept = done.steps[1].changes[0]
    assert (kept.agent_value, kept.current) == ("Closing as stale (live test).", edited)
    assert done.verification["github"].ok is True, done.verification

    issue_a, issue_b = await gh.get(f"issues/{a}"), await gh.get(f"issues/{b}")
    assert issue_a["state"] == issue_b["state"] == "open"
    assert issue_a["title"] == "Teammate renamed this"
    a_comments = await gh.paginate(f"issues/{a}/comments")
    assert not any("live test" in c["body"] for c in a_comments)
    assert any(c["body"] == edited for c in await gh.paginate(f"issues/{b}/comments"))
    restored = await gh.get(content_path(seed.PRICING_PATH))
    assert restored["sha"] == pricing["sha"]
    assert base64.b64decode(restored["content"]).decode() == seed.PRICING
