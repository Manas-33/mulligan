"""The GitHub adapter and the demo seed against an in-memory GitHub (no network)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from mulligan.adapters.github import client as client_module
from mulligan.adapters.github.adapter import (
    ABSENT,
    GitHubAdapter,
    field_hash,
    load_seed,
)
from mulligan.adapters.github.client import GitHubWriteUnknown
from mulligan.core import new_id
from mulligan.interfaces import ToolCall, WriteUncertain
from mulligan.models import (
    Action,
    ActionStatus,
    Agent,
    AgentKind,
    FieldChangeOutcome,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    RewindStep,
    StepResult,
    System,
)
from tests.github_fake import FakeGitHub, blob_sha

AGENT = Agent(id="claude_code", kind=AgentKind.claude_code, display_name="Claude Code")
T0 = datetime(2026, 10, 17, tzinfo=UTC)
SESSION = new_id("ses")


@pytest.fixture
def gh() -> FakeGitHub:
    fake = FakeGitHub()
    for n in range(1, 4):
        fake.add_issue(f"Stale issue {n}", f"Body {n}", ["stale", "bug"])
    fake.put_file("docs/pricing.md", "Widget: $49\n")
    return fake


@pytest.fixture
async def adapter(gh: FakeGitHub) -> AsyncIterator[GitHubAdapter]:
    a = GitHubAdapter(gh.client(), "main")
    try:
        yield a
    finally:
        await a.close()


def tool_call(tool: str, args: dict[str, Any]) -> ToolCall:
    return ToolCall(
        action_id=new_id("act"),
        session_id=SESSION,
        agent=AGENT,
        tool=tool,
        system=System.github,
        args=args,
    )


def logged(tc: ToolCall, n: int, **fields: Any) -> Action:
    return Action(
        id=tc.action_id,
        session_id=SESSION,
        agent=AGENT,
        system=System.github,
        tool=tc.tool,
        upstream_server=None,
        args_redacted=tc.args,
        reversibility=Reversibility(
            label=ReversibilityLabel.reversible_noisy,
            source=ReversibilitySource.policy,
            reason="test",
        ),
        approval=None,
        hold=None,
        explanation=None,
        sentry_trace_id=None,
        created_at=T0 + timedelta(seconds=n),
        reverted_at=None,
        **fields,
    )


async def call(adapter: GitHubAdapter, tool: str, args: dict[str, Any], n: int = 0) -> Action:
    """What `Gateway.execute` does around one call, without the log."""
    tc = tool_call(tool, args)
    before = await adapter.capture_before(tc)
    result = await adapter.execute(tc, before)
    assert not result.is_error, result.summary
    after = await adapter.capture_after(tc, before, result)
    inverse = await adapter.build_inverse(tc, before, after, result)
    return logged(
        tc,
        n,
        status=ActionStatus.executed,
        summary=result.summary,
        error=None,
        before_ref=before.ref,
        after_ref=after.ref,
        after_hashes=after.after_hashes,
        inverse=inverse,
        executed_at=T0 + timedelta(seconds=n),
    )


async def call_unsure(
    adapter: GitHubAdapter, tool: str, args: dict[str, Any], n: int = 0
) -> Action:
    """A call whose write got no answer: failed, with the inverse it raised, like the core."""
    tc = tool_call(tool, args)
    before = await adapter.capture_before(tc)
    with pytest.raises(WriteUncertain) as info:
        await adapter.execute(tc, before)
    exc = info.value
    return logged(
        tc,
        n,
        status=ActionStatus.failed,
        summary=None,
        error=str(exc),
        before_ref=exc.before_ref,
        after_ref=None,
        after_hashes=exc.after_hashes,
        inverse=exc.inverse,
        executed_at=None,
    )


async def rewind(adapter: GitHubAdapter, actions: list[Action]) -> list[RewindStep]:
    return [await adapter.revert(a) for a in sorted(actions, key=lambda a: a.created_at)[::-1]]


async def test_close_with_comment_then_rewind_reopens_and_deletes_the_comment(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    action = await call(
        adapter, "github_close_issue", {"number": 2, "comment": "Closing as stale."}
    )
    assert action.summary == "Closed issue #2 (completed) with a comment"
    assert gh.issues[2]["state"] == "closed"
    (cid,) = gh.comments
    assert action.after_hashes == {
        "github:issues/2/state": field_hash({"state": "closed", "state_reason": "completed"}),
        f"github:comments/{cid}": field_hash("Closing as stale."),
    }
    assert action.inverse is not None and action.inverse.kind == "github.reopen_issue"
    assert action.inverse.payload["before"] == {"state": "open", "state_reason": None}
    assert await adapter.checksum() != pre

    writes = len(gh.writes)
    plan = await adapter.plan_revert(action)
    assert plan.result is StepResult.pending and len(gh.writes) == writes  # no side effects
    assert [c.outcome for c in plan.changes] == [FieldChangeOutcome.restore] * 2

    (step,) = await rewind(adapter, [action])
    assert step.result is StepResult.restored, step.detail
    assert gh.issues[2]["state"] == "open" and not gh.comments
    # GitHub records "reopened"; an open issue's reason doesn't count, so the checksum matches.
    assert gh.issues[2]["state_reason"] == "reopened"
    assert await adapter.checksum() == pre
    expected, actual = await adapter.verify_scope([action], [step]) or ("", "?")
    assert expected == actual


async def test_human_edit_on_the_agents_comment_is_kept_and_flagged(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    action = await call(adapter, "github_close_issue", {"number": 1, "comment": "Stale."})
    (cid,) = gh.comments
    gh.comments[cid]["body"] = "Stale. (Teammate: actually keep this note.)"

    (step,) = await rewind(adapter, [action])
    assert step.result is StepResult.kept_human_edit
    assert step.kept_fields == [f"github:comments/{cid}"]
    kept = step.changes[0]
    assert (kept.before, kept.agent_value, kept.current) == (
        None,
        "Stale.",
        "Stale. (Teammate: actually keep this note.)",
    )
    assert gh.issues[1]["state"] == "open" and cid in gh.comments
    expected, actual = await adapter.verify_scope([action], [step]) or ("", "?")
    assert expected == actual


async def test_human_reclose_keeps_state_and_unrelated_title_edit_does_not_fail_verification(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    close1 = await call(adapter, "github_close_issue", {"number": 1}, 0)
    close3 = await call(adapter, "github_close_issue", {"number": 3}, 1)
    gh.issues[1]["title"] = "Renamed by a teammate"  # a field the agent never touched
    gh.issues[3]["state_reason"] = "not_planned"  # a teammate re-triaged it after the agent

    steps = await rewind(adapter, [close1, close3])
    by_tool = {s.action_id: s for s in steps}
    assert by_tool[close1.id].result is StepResult.restored
    assert gh.issues[1]["state"] == "open"
    assert gh.issues[1]["title"] == "Renamed by a teammate"
    s3 = by_tool[close3.id]
    assert s3.result is StepResult.kept_human_edit
    assert s3.kept_fields == ["github:issues/3/state"]
    assert s3.changes[0].current == {"state": "closed", "state_reason": "not_planned"}
    assert gh.issues[3]["state"] == "closed"

    assert await adapter.checksum() != pre  # the whole repo differs (title, issue 3) ...
    expected, actual = await adapter.verify_scope([close1, close3], steps) or ("", "?")
    assert expected == actual  # ... but everything the rewind owns is where it should be


async def test_file_update_rewinds_through_the_contents_api(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    old_sha = blob_sha(gh.files["docs/pricing.md"])
    update = await call(
        adapter,
        "github_update_file",
        {"path": "docs/pricing.md", "content": "Widget: $0\n", "message": "Simplify pricing"},
        0,
    )
    create = await call(
        adapter,
        "github_update_file",
        {"path": "docs/new.md", "content": "hello\n", "message": "Add notes"},
        1,
    )
    new_sha = blob_sha(b"Widget: $0\n")
    assert update.after_hashes == {"github:files/docs%2Fpricing.md": field_hash(new_sha)}
    assert update.inverse is not None
    assert update.inverse.payload["before_sha"] == old_sha
    assert update.inverse.payload["before_text"] == "Widget: $49\n"
    assert create.inverse is not None and create.inverse.payload["before_sha"] is None

    steps = await rewind(adapter, [update, create])
    assert [s.result for s in steps] == [StepResult.restored, StepResult.restored]
    assert "Delete docs/new.md" in (steps[0].detail or "")
    assert gh.files["docs/pricing.md"] == b"Widget: $49\n" and "docs/new.md" not in gh.files
    assert await adapter.checksum() == pre
    expected, actual = await adapter.verify_scope([update, create], steps) or ("", "?")
    assert expected == actual


async def test_teammate_file_edit_is_kept(adapter: GitHubAdapter, gh: FakeGitHub) -> None:
    update = await call(
        adapter,
        "github_update_file",
        {"path": "docs/pricing.md", "content": "Widget: $0\n", "message": "Simplify"},
    )
    gh.put_file("docs/pricing.md", "Widget: $45\n")
    plan = await adapter.plan_revert(update)
    assert plan.kept_fields == ["github:files/docs%2Fpricing.md"]
    (step,) = await rewind(adapter, [update])
    assert step.result is StepResult.kept_human_edit
    change = step.changes[0]
    assert change.before == {"blob_sha": blob_sha(b"Widget: $49\n"), "text": "Widget: $49\n"}
    assert change.current == {"blob_sha": blob_sha(b"Widget: $45\n"), "text": "Widget: $45\n"}
    assert gh.files["docs/pricing.md"] == b"Widget: $45\n"
    expected, actual = await adapter.verify_scope([update], [step]) or ("", "?")
    assert expected == actual


async def test_comment_tool_and_deleted_comment(adapter: GitHubAdapter, gh: FakeGitHub) -> None:
    action = await call(adapter, "github_comment", {"number": 3, "body": "Ping?"})
    (cid,) = gh.comments
    assert action.inverse is not None and action.inverse.kind == "github.delete_comment"
    del gh.comments[cid]  # someone already deleted it: nothing left to do
    (step,) = await rewind(adapter, [action])
    assert step.result is StepResult.restored and step.detail == "Nothing to restore."
    assert await adapter.verify_scope([action], [step]) is not None


async def test_list_issues_filters_and_skips_pull_requests(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    n = gh.add_issue("A pull request", labels=["stale"])
    gh.issues[n]["pull_request"] = {"url": "x"}
    gh.add_issue("Active one", labels=["bug"])
    action = await call(adapter, "github_list_issues", {"labels": ["stale"], "per_page": 50})
    assert action.inverse is None
    tc = ToolCall(
        action_id=new_id("act"),
        session_id="s",
        agent=AGENT,
        tool="github_list_issues",
        system=System.github,
        args={"labels": ["stale"]},
    )
    result = await adapter.execute(tc, await adapter.capture_before(tc))
    issues = result.structured_content["issues"]
    assert [i["number"] for i in issues] == [3, 2, 1]
    assert issues[0]["labels"] == ["bug", "stale"] and issues[0]["state"] == "open"
    assert result.summary == "Listed 3 open issues"


async def test_bad_input_is_a_tool_error(adapter: GitHubAdapter) -> None:
    tc = ToolCall(
        action_id=new_id("act"),
        session_id="s",
        agent=AGENT,
        tool="github_comment",
        system=System.github,
        args={"number": 99, "body": "hi"},
    )
    result = await adapter.execute(tc, await adapter.capture_before(tc))
    assert result.is_error and "404" in result.summary
    with pytest.raises(Exception, match="bad path"):
        await adapter.capture_before(
            tc.model_copy(
                update={"tool": "github_update_file", "args": {"path": "../x", "content": ""}}
            )
        )


async def test_rate_limited_request_waits_and_retries(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    waits: list[float] = []

    async def no_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(client_module.asyncio, "sleep", no_sleep)
    gh.fail = [
        httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"}),
        httpx.Response(429, headers={"retry-after": "7"}, json={"message": "slow down"}),
    ]
    client = gh.client()
    try:
        issue = await client.get("issues/1")
    finally:
        await client.aclose()
    assert issue["number"] == 1
    assert waits == [60.0, 7.0]


async def test_writes_are_paced(gh: FakeGitHub) -> None:
    client = gh.client()
    client.write_interval = 0.05
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        for n in (1, 2, 3):
            await client.send("PATCH", f"issues/{n}", {"state": "closed"})
    finally:
        await client.aclose()
    assert loop.time() - start >= 0.1


def test_absent_hash_matches_the_contract() -> None:
    assert ABSENT == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert field_hash(None) == field_hash(None) != ABSENT
    assert field_hash({"b": 1, "a": [2]}) == field_hash({"a": [2], "b": 1})


# --- the demo seed ------------------------------------------------------------------------------


async def test_seed_is_idempotent_and_reset_undoes_drift() -> None:
    seed = load_seed()
    gh = FakeGitHub()
    client = gh.client()
    try:
        first = await seed.apply(client)
        assert len(first.numbers) == 45 and len(gh.issues) == 45
        assert seed.stale_numbers(first) == [
            n for n in range(1, 46) if n not in (5, 14, 23, 32, 41)
        ]
        assert gh.files["docs/pricing.md"].startswith(b"# Acme Widgets pricing")
        assert len(gh.comments) == 5
        adapter = GitHubAdapter(client, "main")
        clean = await adapter.checksum()

        again = await seed.apply(client)
        assert again.actions == [] and again.numbers == first.numbers

        # The rogue agent's kind of damage, plus a teammate's edits.
        gh.issues[1].update(state="closed", state_reason="completed", title="x")
        gh.issues[2]["labels"] = []
        await client.send("POST", "issues/3/comments", {"body": "closing as stale"})
        seed_comment = next(iter(gh.comments))
        gh.comments[seed_comment]["body"] = "edited"
        gh.put_file("docs/pricing.md", "free!\n")
        assert await adapter.checksum() != clean

        done = await seed.reset(client)
        assert len(done.actions) == 6, done.actions  # the edited seed comment is re-added
        assert await seed.apply(client, write=False) == seed.Report(numbers=first.numbers)
        assert len(gh.issues) == 45
        assert await adapter.checksum() == clean
    finally:
        await client.aclose()


async def test_comment_whose_reply_was_lost_is_found_and_deleted(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    gh.lose.append("POST issues/1/comments")
    action = await call_unsure(adapter, "github_comment", {"number": 1, "body": "Bumping this."})
    (cid,) = gh.comments  # GitHub applied it; only the answer was lost
    assert gh.writes == ["POST issues/1/comments"]  # and it was never sent again
    assert action.inverse is not None and action.inverse.payload["comment"] is None

    writes = len(gh.writes)
    plan = await adapter.plan_revert(action)
    assert plan.result is StepResult.pending and len(gh.writes) == writes
    assert [(c.field, c.outcome) for c in plan.changes] == [
        (f"github:comments/{cid}", FieldChangeOutcome.restore)
    ]
    step = await adapter.revert(action)
    assert step.result is StepResult.restored and step.detail == f"Delete comment {cid}"
    assert not gh.comments
    assert await adapter.checksum() == pre
    expected, actual = await adapter.verify_scope([action], [step]) or ("", "?")
    assert expected == actual


async def test_comment_that_never_landed_leaves_nothing_to_undo(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    gh.fail.append(httpx.ConnectError("connection refused"))
    action = await call_unsure(adapter, "github_comment", {"number": 1, "body": "Bumping this."})
    assert not gh.comments
    # A teammate posts the same words much later: not the agent's comment.
    gh.next_id += 1
    later = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    gh.comments[gh.next_id] = {
        "id": gh.next_id,
        "body": "Bumping this.",
        "created_at": later,
        "updated_at": later,
        "_n": 1,
    }
    step = await adapter.revert(action)
    assert step.result is StepResult.restored and step.detail == "Nothing to restore."
    assert list(gh.comments) == [gh.next_id] and not gh.writes


async def test_close_whose_reply_was_lost_reopens_and_deletes_the_comment(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    gh.lose.append("PATCH issues/2")
    action = await call_unsure(
        adapter, "github_close_issue", {"number": 2, "comment": "Closing as stale."}
    )
    (cid,) = gh.comments  # kept: the close may have landed, so a rewind undoes both
    assert gh.issues[2]["state"] == "closed"
    assert action.after_hashes == {
        "github:issues/2/state": field_hash({"state": "closed", "state_reason": "completed"}),
        f"github:comments/{cid}": field_hash("Closing as stale."),
    }
    step = await adapter.revert(action)
    assert step.result is StepResult.restored, step.detail
    assert gh.issues[2]["state"] == "open" and not gh.comments
    assert await adapter.checksum() == pre


async def test_file_update_whose_reply_was_lost_is_restored(
    adapter: GitHubAdapter, gh: FakeGitHub
) -> None:
    pre = await adapter.checksum()
    gh.lose.append("PUT contents/docs/pricing.md")
    action = await call_unsure(
        adapter,
        "github_update_file",
        {"path": "docs/pricing.md", "content": "Everything is free\n", "message": "Simplify"},
    )
    assert gh.files["docs/pricing.md"] == b"Everything is free\n"
    assert action.after_hashes == {
        "github:files/docs%2Fpricing.md": field_hash(blob_sha(b"Everything is free\n"))
    }
    step = await adapter.revert(action)
    assert step.result is StepResult.restored, step.detail
    assert gh.files["docs/pricing.md"] == b"Widget: $49\n"
    assert await adapter.checksum() == pre


async def test_write_answered_with_a_5xx_is_unknown_and_not_resent(gh: FakeGitHub) -> None:
    client = gh.client()
    gh.fail.append(httpx.Response(502, json={"message": "Bad Gateway"}))
    with pytest.raises(GitHubWriteUnknown):
        await client.send("POST", "issues/1/comments", {"body": "hi"})
    assert gh.requests == ["POST /repos/acme/widgets/issues/1/comments"]
    await client.aclose()
