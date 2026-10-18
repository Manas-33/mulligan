"""The pitch's Notion scenario through the gateway and the Rewinder (needs `make db`).

The rogue agent rewrites two blocks, retitles the page, appends a block and trashes the page; a
teammate fixes one of the rewritten prices by hand in between. The rewind keeps and flags that
price, puts everything else back with the same block ids, and the scoped verification passes.

`test_rewind_through_the_gateway` runs it against the in-memory Notion. `test_live_rewind` runs it
against the real pricing page (MULLIGAN_LIVE_NOTION=1, NOTION_TOKEN and NOTION_PRICING_PAGE_ID
set; `uv run pytest -m live`) and leaves the page as it found it.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

import pytest

from mulligan.adapters.notion.adapter import NotionAdapter, canonical_hash, flat, record
from mulligan.adapters.notion.client import NotionClient
from mulligan.adapters.notion.demo import teammate_edit
from mulligan.adapters.notion.normalize import block_form, form_text
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    ActionStatus,
    Agent,
    AgentKind,
    FieldChangeOutcome,
    RewindJobStatus,
    RewindRequest,
    RewindScope,
    SessionMode,
    StepResult,
)
from mulligan.services import Services, assemble
from mulligan.settings import Settings
from tests.notion_fake import FakeNotion

AGENT = Agent(id="rogue", kind=AgentKind.rogue, display_name="Cleanup agent")
LIVE = os.environ.get("MULLIGAN_LIVE_NOTION") == "1"


async def services(log_pool: Any, adapter: NotionAdapter) -> Services:
    return assemble(Settings(), LogStore(log_pool), EventBus(log_pool), [adapter])


def by_text(snap: dict[str, Any]) -> dict[str, str]:
    return {form_text(block_form(n)) or "": n["id"] for n in flat(snap["blocks"])}


async def scenario(svc: Services, adapter: NotionAdapter) -> None:
    page_id = adapter.default_page_id
    assert page_id
    original = await adapter.snapshot(page_id)
    blocks = by_text(original)
    pro, basic = blocks["Pro: $99/month"], blocks["Widget Basic | $12"]

    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    call = svc.gateway.call_tool
    read = await call(session, "notion_get_page", {})
    assert not read.is_error, read.summary
    updated = await call(
        session,
        "notion_update_page",
        {
            "page_id": page_id,
            "properties": {"title": "Acme Widgets — Pricing (v2, doubled)"},
            "block_updates": [
                {"block_id": pro, "text": "Pro: $199/month"},
                {"block_id": basic, "text": "Widget Basic | $24"},
            ],
            "append_blocks": [{"type": "paragraph", "text": "New: Ultra plan, $999/month."}],
        },
    )
    assert not updated.is_error, updated.summary
    assert updated.structured_content["updated_block_ids"] == [pro, basic]
    ultra = updated.structured_content["appended_block_ids"][0]

    # A teammate fixes the Basic price straight in Notion, before the page goes to the trash.
    line = await teammate_edit(adapter, "Widget Basic", "$15")
    assert line.endswith("$24 -> $15")

    trashed = await call(session, "notion_trash_page", {"page_id": page_id})
    assert not trashed.is_error, trashed.summary
    assert (await adapter.client.get_page(page_id))["in_trash"]

    plan = await svc.rewinder.create(RewindRequest(scope=RewindScope(session_id=session.id)))
    kept_key = f"notion:{page_id}/blocks/{basic}"
    assert [s.tool for s in plan.steps] == ["notion_trash_page", "notion_update_page"]
    assert plan.steps[1].kept_fields == [kept_key]
    assert (await adapter.client.get_page(page_id))["in_trash"]  # the dry run changed nothing

    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None
    assert done.status is RewindJobStatus.completed, done
    untrash, update = done.steps
    assert untrash.result is StepResult.restored
    assert update.result is StepResult.kept_human_edit
    assert update.kept_fields == [kept_key]
    kept = update.changes[0]
    assert kept.outcome is FieldChangeOutcome.keep_human_edit
    assert (kept.before, kept.agent_value, kept.current) == (
        "Widget Basic | $12",
        "Widget Basic | $24",
        "Widget Basic | $15",
    )
    assert all(c.outcome is FieldChangeOutcome.restore for c in update.changes[1:])
    assert done.verification["notion"].ok is True
    action = await svc.store.get(update.action_id)
    assert action is not None and action.status is ActionStatus.conflict

    # Everything but the teammate's price matches the original, with the same block ids.
    now = record(await adapter.snapshot(page_id))
    want = record(original)
    assert [b["id"] for b in now["blocks"]] == [b["id"] for b in want["blocks"]]
    assert ultra not in by_text(await adapter.snapshot(page_id)).values()
    assert now["properties"] == want["properties"] and not now["in_trash"]
    rows_now = {b["id"]: b for b in flat(now["blocks"])}
    for b in flat(want["blocks"]):
        if b["id"] == basic:
            assert form_text(rows_now[basic]) == "Widget Basic | $15"
        else:
            assert {k: rows_now[b["id"]][k] for k in ("type", "body")} == {
                k: b[k] for k in ("type", "body")
            }


@pytest.mark.integration
async def test_rewind_through_the_gateway(log_pool: Any) -> None:
    fake = FakeNotion()
    adapter = NotionAdapter(fake.client(), fake.page_id)
    svc = await services(log_pool, adapter)
    await scenario(svc, adapter)
    await svc.rewinder.wait()


@pytest.fixture
async def live_adapter() -> AsyncIterator[NotionAdapter]:
    token, page = os.environ.get("NOTION_TOKEN", ""), os.environ.get("NOTION_PRICING_PAGE_ID", "")
    if not (token and page):
        pytest.skip("NOTION_TOKEN and NOTION_PRICING_PAGE_ID are needed")
    adapter = NotionAdapter(NotionClient(token), page)
    snap = await adapter.snapshot(adapter.default_page_id or "")
    if canonical_hash(snap) != canonical_hash_of_seed(adapter):
        await adapter.close()
        pytest.skip("the pricing page doesn't match demo/seed/notion_pricing.json; reset it first")
    try:
        yield adapter
    finally:
        await adapter.reset()  # back to the seed, which is how the test found it
        assert canonical_hash(await adapter.snapshot(adapter.default_page_id or "")) == (
            canonical_hash(snap)
        )
        await adapter.close()


def canonical_hash_of_seed(adapter: NotionAdapter) -> str:
    """The seed file as a snapshot, so it hashes like the live page."""
    seed = json.loads(adapter.seed_path.read_text())

    def nodes(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": n["id"],
                "type": n["type"],
                n["type"]: n["body"],
                "children": nodes(n["children"]),
            }
            for n in items
        ]

    page = {
        "in_trash": seed["in_trash"],
        "properties": seed["properties"],
        "icon": None,
        "cover": None,
    }
    return canonical_hash({"page": page, "blocks": nodes(seed["blocks"])})


@pytest.mark.live
@pytest.mark.skipif(not LIVE, reason="set MULLIGAN_LIVE_NOTION=1 to run against the real page")
async def test_live_rewind(log_pool: Any, live_adapter: NotionAdapter) -> None:
    svc = await services(log_pool, live_adapter)
    await scenario(svc, live_adapter)
    await svc.rewinder.wait()
