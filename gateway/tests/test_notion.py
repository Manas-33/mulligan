"""The Notion adapter against an in-memory Notion API (tests/notion_fake.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from mulligan.adapters.notion.adapter import NotionAdapter, canonical_hash
from mulligan.adapters.notion.client import NotionClient, NotionError, normalize_id
from mulligan.interfaces import ToolCall, ToolResult
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
from tests.notion_fake import FakeNotion

AGENT = Agent(id="rogue", kind=AgentKind.rogue, display_name="Cleanup agent")
INTRO = "Prices for Acme Widgets plans, effective October 2026."


@pytest.fixture
def fake() -> FakeNotion:
    return FakeNotion()


@pytest.fixture
async def adapter(fake: FakeNotion) -> Any:
    a = NotionAdapter(fake.client(), fake.page_id)
    yield a
    await a.close()


class Runner:
    """Runs calls through an adapter the way the gateway does and keeps the logged actions."""

    def __init__(self, adapter: NotionAdapter) -> None:
        self.adapter = adapter
        self.actions: list[Action] = []
        self.t0 = datetime(2026, 10, 18, tzinfo=UTC)

    async def call(self, tool: str, args: dict[str, Any]) -> tuple[ToolResult, Action]:
        n = len(self.actions)
        action_id = f"act_{n:026d}"
        call = ToolCall(
            action_id=action_id,
            session_id="ses_" + "0" * 26,
            agent=AGENT,
            tool=tool,
            system=System.notion,
            args=args,
        )
        a = self.adapter
        before = await a.capture_before(call)
        result = await a.execute(call, before)
        after = await a.capture_after(call, before, result)
        inverse = None if result.is_error else await a.build_inverse(call, before, after, result)
        action = Action(
            id=action_id,
            session_id=call.session_id,
            agent=AGENT,
            system=System.notion,
            tool=tool,
            upstream_server=None,
            args_redacted=args,
            status=ActionStatus.failed if result.is_error else ActionStatus.executed,
            reversibility=Reversibility(
                label=ReversibilityLabel.reversible, source=ReversibilitySource.policy, reason=None
            ),
            summary=result.summary,
            error=None,
            before_ref=before.ref,
            after_ref=after.ref,
            after_hashes=after.after_hashes,
            inverse=inverse,
            approval=None,
            hold=None,
            explanation=None,
            sentry_trace_id=None,
            created_at=self.t0 + timedelta(seconds=n),
            executed_at=None,
            reverted_at=None,
        )
        if not result.is_error:
            self.actions.append(action)
        return result, action

    async def rewind(self, *, apply: bool = True) -> list[RewindStep]:
        steps = []
        for action in reversed(self.actions):
            step = (
                await self.adapter.revert(action)
                if apply
                else await self.adapter.plan_revert(action)
            )
            steps.append(step)
        return steps


async def rogue(fake: FakeNotion, run: Runner) -> dict[str, str]:
    """The pitch's rogue edit: rewrite two blocks and a table row, retitle, append, trash."""
    ids = {
        "intro": fake.find(INTRO),
        "pro": fake.find("Pro: $99/month"),
        "basic": fake.find("Widget Basic | $12"),
    }
    result, _ = await run.call(
        "notion_update_page",
        {
            "page_id": fake.page_id,
            "properties": {"title": "Acme Widgets — Pricing (v2, doubled)"},
            "block_updates": [
                {"block_id": ids["intro"], "text": "All prices doubled, effective now."},
                {"block_id": ids["pro"], "text": "Pro: $199/month"},
                {"block_id": ids["basic"], "text": "Widget Basic | $24"},
            ],
            "append_blocks": [{"type": "paragraph", "text": "New: Ultra plan, $999/month."}],
        },
    )
    assert not result.is_error, result.summary
    ids["ultra"] = result.structured_content["appended_block_ids"][0]
    result, _ = await run.call("notion_trash_page", {"page_id": fake.page_id})
    assert not result.is_error, result.summary
    return ids


async def test_get_page_is_simplified(adapter: NotionAdapter, fake: FakeNotion) -> None:
    run = Runner(adapter)
    result, _ = await run.call("notion_get_page", {})
    page = result.structured_content
    assert page["title"] == "Acme Widgets — Pricing"
    assert page["properties"] == {"title": "Acme Widgets — Pricing"}
    assert page["in_trash"] is False
    texts = [b["text"] for b in page["blocks"]]
    assert texts[:3] == ["Pricing", INTRO, "Starter: $29/month"]
    assert "Widget Basic | $12" in texts
    table = next(b for b in page["blocks"] if b["type"] == "table")
    assert table["has_children"] is True
    assert run.actions[0].inverse is None


async def test_rewind_restores_the_page_in_place(adapter: NotionAdapter, fake: FakeNotion) -> None:
    original = canonical_hash(await adapter.snapshot(fake.page_id))
    order = fake.ids()
    run = Runner(adapter)
    ids = await rogue(fake, run)

    update = run.actions[0]
    pid = fake.page_id
    assert set(update.after_hashes) == {
        f"notion:{pid}/properties/title",
        *(f"notion:{pid}/blocks/{ids[k]}" for k in ("intro", "pro", "basic", "ultra")),
    }
    assert all(len(h) == 64 for h in update.after_hashes.values())
    assert run.actions[1].after_hashes.keys() == {f"notion:{pid}"}
    assert update.inverse is not None and "snapshot" in update.inverse.payload
    assert run.actions[1].inverse is not None and "snapshot" not in run.actions[1].inverse.payload
    assert fake.page["in_trash"]

    plan = await run.rewind(apply=False)
    assert all(s.result is StepResult.pending for s in plan)
    assert not any(s.kept_fields for s in plan)  # blocks read in_trash while the page is trashed
    assert fake.page["in_trash"]  # the dry run changed nothing

    steps = await run.rewind()
    assert [s.result for s in steps] == [StepResult.restored, StepResult.restored]
    assert steps[0].detail == "Untrashed the page."
    assert steps[1].detail == "Restored 1 property and 3 blocks; deleted 1 appended block."
    assert not fake.page["in_trash"]
    assert canonical_hash(await adapter.snapshot(pid)) == original
    assert fake.ids() == order  # same blocks, same ids, same order
    assert fake.blocks[ids["ultra"]]["in_trash"]
    expected, actual = await adapter.verify_scope(run.actions, steps) or ("", "?")
    assert expected == actual


async def test_teammate_edit_is_kept_and_flagged(adapter: NotionAdapter, fake: FakeNotion) -> None:
    original = await adapter.snapshot(fake.page_id)
    run = Runner(adapter)
    ids = {"basic": fake.find("Widget Basic | $12"), "pro": fake.find("Pro: $99/month")}
    await run.call(
        "notion_update_page",
        {
            "page_id": fake.page_id,
            "block_updates": [
                {"block_id": ids["basic"], "text": "Widget Basic | $24"},
                {"block_id": ids["pro"], "text": "Pro: $199/month"},
            ],
        },
    )
    fake.human_edit(ids["basic"], "Widget Basic | $15")
    fake.human_edit(fake.find("Starter: $29/month"), "Starter: $25/month")  # never touched

    plan = await run.rewind(apply=False)
    key = f"notion:{fake.page_id}/blocks/{ids['basic']}"
    assert plan[0].kept_fields == [key]

    steps = await run.rewind()
    step = steps[0]
    assert step.result is StepResult.kept_human_edit
    assert step.kept_fields == [key]
    kept = step.changes[0]
    assert kept.outcome is FieldChangeOutcome.keep_human_edit
    assert (kept.before, kept.agent_value, kept.current) == (
        "Widget Basic | $12",
        "Widget Basic | $24",
        "Widget Basic | $15",
    )
    assert step.changes[1].outcome is FieldChangeOutcome.restore
    assert fake.text(ids["basic"]) == "Widget Basic | $15"
    assert fake.text(ids["pro"]) == "Pro: $99/month"
    # The edit elsewhere on the page changes the page checksum but not the scoped check.
    assert canonical_hash(await adapter.snapshot(fake.page_id)) != canonical_hash(original)
    expected, actual = await adapter.verify_scope(run.actions, steps) or ("", "?")
    assert expected == actual


async def test_scoped_verification_catches_a_bad_restore(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    run = Runner(adapter)
    pro = fake.find("Pro: $99/month")
    await run.call(
        "notion_update_page",
        {"page_id": fake.page_id, "block_updates": [{"block_id": pro, "text": "Pro: $1"}]},
    )
    steps = await run.rewind()
    fake.human_edit(pro, "Pro: $2")  # not kept by the rewind, so the check must fail
    expected, actual = await adapter.verify_scope(run.actions, steps) or ("", "")
    assert expected != actual


async def test_two_actions_on_one_block_rewind_to_the_first_before(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    run = Runner(adapter)
    pro = fake.find("Pro: $99/month")
    for text in ("Pro: $149/month", "Pro: $199/month"):
        await run.call(
            "notion_update_page",
            {"page_id": fake.page_id, "block_updates": [{"block_id": pro, "text": text}]},
        )
    steps = await run.rewind()
    assert all(s.result is StepResult.restored for s in steps)
    assert fake.text(pro) == "Pro: $99/month"
    expected, actual = await adapter.verify_scope(run.actions, steps) or ("", "?")
    assert expected == actual


async def test_human_deleting_an_appended_block_is_fine(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    run = Runner(adapter)
    result, _ = await run.call(
        "notion_update_page",
        {"page_id": fake.page_id, "append_blocks": [{"type": "to_do", "text": "Call sales"}]},
    )
    new = result.structured_content["appended_block_ids"][0]
    assert fake.blocks[new]["to_do"]["checked"] is False
    fake.blocks[new]["in_trash"] = True
    steps = await run.rewind()
    assert steps[0].result is StepResult.restored
    assert steps[0].detail == "Nothing to restore."


async def test_trashing_a_workspace_level_page_is_refused(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    fake.page["parent"] = {"type": "workspace", "workspace": True}
    result, _ = await Runner(adapter).call("notion_trash_page", {"page_id": fake.page_id})
    assert result.is_error
    assert "workspace-level" in result.summary
    assert not fake.page["in_trash"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "at least one of"),
        ({"properties": {"Owner": "x"}}, "no property 'Owner'"),
        ({"block_updates": [{"block_id": "0" * 32, "text": "x"}]}, "isn't on this page"),
        ({"append_blocks": [{"type": "image", "text": "x"}]}, "can't append"),
        ({"properties": {"title": "ok"}, "append_blocks": [{"type": "x", "text": ""}]}, "append"),
    ],
)
async def test_bad_updates_are_refused_before_any_write(
    adapter: NotionAdapter, fake: FakeNotion, args: dict[str, Any], message: str
) -> None:
    result, _ = await Runner(adapter).call("notion_update_page", {"page_id": fake.page_id, **args})
    assert result.is_error
    assert message in result.summary
    assert all(method == "GET" for method, _ in fake.log)


async def test_table_row_needs_every_cell(adapter: NotionAdapter, fake: FakeNotion) -> None:
    row = fake.find("Widget Basic | $12")
    result, _ = await Runner(adapter).call(
        "notion_update_page",
        {"page_id": fake.page_id, "block_updates": [{"block_id": row, "text": "$24"}]},
    )
    assert result.is_error and "2 cells" in result.summary


async def test_a_failed_write_rolls_back_the_call(adapter: NotionAdapter, fake: FakeNotion) -> None:
    pro = fake.find("Pro: $99/month")

    def fail(request: httpx.Request) -> httpx.Response | None:
        if request.method == "PATCH" and request.url.path.endswith(pro):
            return httpx.Response(400, json={"code": "validation_error", "message": "nope"})
        return None

    fake.fail = fail
    result, action = await Runner(adapter).call(
        "notion_update_page",
        {
            "page_id": fake.page_id,
            "properties": {"title": "Changed"},
            "block_updates": [{"block_id": pro, "text": "Pro: $1"}],
        },
    )
    assert result.is_error and "rolled back" in result.summary
    assert fake.page["properties"]["title"]["title"][0]["plain_text"] == "Acme Widgets — Pricing"


async def test_editing_a_trashed_page_is_refused(adapter: NotionAdapter, fake: FakeNotion) -> None:
    fake.page["in_trash"] = True
    result, _ = await Runner(adapter).call(
        "notion_update_page", {"page_id": fake.page_id, "properties": {"title": "x"}}
    )
    assert result.is_error and "trash" in result.summary


async def test_revert_on_a_page_someone_else_trashed_fails_cleanly(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    run = Runner(adapter)
    pro = fake.find("Pro: $99/month")
    await run.call(
        "notion_update_page",
        {"page_id": fake.page_id, "block_updates": [{"block_id": pro, "text": "Pro: $1"}]},
    )
    fake.page["in_trash"] = True
    steps = await run.rewind()
    assert steps[0].result is StepResult.failed
    assert steps[0].detail and "in the trash" in steps[0].detail


async def test_checksum_is_cached_for_the_capture_after_it(
    adapter: NotionAdapter, fake: FakeNotion
) -> None:
    await adapter.checksum()
    reads = len(fake.log)
    run = Runner(adapter)
    await run.call(
        "notion_update_page", {"page_id": fake.page_id, "properties": {"title": "Pricing v2"}}
    )
    assert [m for m, _ in fake.log[reads:]] == ["PATCH"]


async def test_reset_puts_the_seed_back(adapter: NotionAdapter, fake: FakeNotion) -> None:
    original = canonical_hash(await adapter.snapshot(fake.page_id))
    order = fake.ids()
    run = Runner(adapter)
    await rogue(fake, run)
    fake.page["in_trash"] = False
    enterprise = fake.find("Enterprise: $499/month")
    fake.blocks[enterprise]["in_trash"] = True  # a person deleted a block
    fake.page["in_trash"] = True
    await adapter.reset()
    assert canonical_hash(await adapter.snapshot(fake.page_id)) == original
    assert fake.ids() == order


def test_normalize_id() -> None:
    url = "https://www.notion.so/acme/Acme-Widgets-Pricing-0123456789abcdef0123456789abcdef?pvs=4"
    assert normalize_id(url) == "01234567-89ab-cdef-0123-456789abcdef"
    assert normalize_id("01234567-89ab-cdef-0123-456789abcdef") == normalize_id(url)
    with pytest.raises(ValueError):
        normalize_id("pricing")


async def test_client_retries_rate_limits_and_read_timeouts() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": "rate_limited"})
        if len(calls) == 2:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, json={"object": "page", "id": "p"})

    client = NotionClient("t", transport=httpx.MockTransport(handle), min_interval_s=0)
    assert (await client.get_page("p"))["id"] == "p"
    assert calls == ["GET", "GET", "GET"]


async def test_client_never_resends_an_append_that_may_have_run() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = NotionClient("t", transport=httpx.MockTransport(handle), min_interval_s=0)
    with pytest.raises(httpx.ReadTimeout):
        await client.append("p", [{"type": "paragraph", "paragraph": {"rich_text": []}}])


async def test_client_raises_notion_errors() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"code": "object_not_found", "message": "gone"})

    client = NotionClient("t", transport=httpx.MockTransport(handle), min_interval_s=0)
    with pytest.raises(NotionError) as err:
        await client.get_block("b")
    assert err.value.not_found
