"""Upstream MCP pass-through against the mock Slack server, run in process."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from mulligan.interfaces import Classifier, ToolCall, UpstreamToolSchema
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.mcp_server.server import build_mcp
from mulligan.models import (
    ActionStatus,
    Agent,
    AgentKind,
    ApprovalChannel,
    ApprovalDecision,
    ApprovalDecisionRequest,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    RewindRequest,
    RewindScope,
    SessionMode,
    StepResult,
    System,
)
from mulligan.services import Services, assemble
from mulligan.settings import Settings
from mulligan.upstream import UpstreamAdapter, exposed_name, parse_servers

SLACK_PATH = Path(__file__).resolve().parents[2] / "demo/upstream/slack_server.py"
spec = importlib.util.spec_from_file_location("slack_server", SLACK_PATH)
assert spec and spec.loader
slack_server: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slack_server)

AGENT = Agent(id="claude_code", kind=AgentKind.claude_code, display_name="Claude Code")


class ReadVerbClassifier(Classifier):
    """Stand-in for Stream B's rules gate: read verbs pass, everything else is held."""

    def __init__(self) -> None:
        self.seen: list[UpstreamToolSchema] = []
        self.fail = False

    def classify_unknown(self, tool_schema: UpstreamToolSchema) -> Reversibility:
        self.seen.append(tool_schema)
        if self.fail:
            raise RuntimeError("boom")
        read = tool_schema.name.startswith(("get", "list", "search", "read"))
        return Reversibility(
            label=ReversibilityLabel.read_only if read else ReversibilityLabel.irreversible,
            source=ReversibilitySource.rules,
            reason="read verb" if read else "not a read verb",
        )


def test_parse_servers() -> None:
    assert parse_servers("slack=http://127.0.0.1:8200/mcp, docs=http://x/mcp") == {
        "slack": "http://127.0.0.1:8200/mcp",
        "docs": "http://x/mcp",
    }
    for bad in ["slack", "Slack=http://x", "a__b=http://x", "slack="]:
        with pytest.raises(ValueError):
            parse_servers(bad)


async def test_discover_keeps_original_names_and_hints() -> None:
    adapter = UpstreamAdapter({"slack": slack_server.slack})
    schemas = await adapter.discover()
    assert set(schemas) == {"upstream__slack__list_channels", "upstream__slack__post_message"}
    post = schemas["upstream__slack__post_message"]
    assert (post.server, post.name) == ("slack", "post_message")
    assert set(post.input_schema["properties"]) == {"channel", "text"}
    listing = schemas["upstream__slack__list_channels"]
    assert listing.annotations is not None and listing.annotations.readOnlyHint is True


async def test_unreachable_upstream_is_skipped() -> None:
    adapter = UpstreamAdapter({"gone": "http://127.0.0.1:9/mcp"})
    assert await adapter.discover() == {}


async def test_an_upstream_that_never_answers_times_out() -> None:
    class Stuck:
        async def __aenter__(self) -> Stuck:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            await asyncio.sleep(10)

    adapter = UpstreamAdapter({"slack": "http://unused"}, connect=lambda _: Stuck(), timeout_s=0.05)  # type: ignore[arg-type,return-value]
    name = exposed_name("slack", "post_message")
    adapter.schemas[name] = UpstreamToolSchema(
        server="slack", name="post_message", exposed_name=name, input_schema={}
    )
    call = ToolCall(
        action_id="act_" + "0" * 26,
        session_id="ses_" + "0" * 26,
        agent=AGENT,
        tool=name,
        system=System.upstream,
        args={},
        upstream_server="slack",
    )
    with pytest.raises(TimeoutError, match="didn't answer within 0.05 s"):
        await adapter.execute(call, await adapter.capture_before(call))


@pytest.fixture
async def upstream_svc(log_pool: Any) -> tuple[Services, ReadVerbClassifier]:
    slack_server.POSTS.clear()
    adapter = UpstreamAdapter({"slack": slack_server.slack})
    await adapter.discover()
    classifier = ReadVerbClassifier()
    svc = assemble(
        Settings(), LogStore(log_pool), EventBus(log_pool), [adapter], classifier=classifier
    )
    svc.gateway.upstream.update(adapter.schemas)
    return svc, classifier


@pytest.mark.integration
async def test_reads_pass_writes_wait_for_approval(upstream_svc: Any) -> None:
    svc, classifier = upstream_svc
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    listed = await svc.gateway.call_tool(session, exposed_name("slack", "list_channels"), {})
    assert listed.structured_content["status"] == "executed"
    assert {c["name"] for c in listed.structured_content["channels"]} >= {"general", "pricing"}

    post = await svc.gateway.call_tool(
        session, "upstream__slack__post_message", {"channel": "#general", "text": "cleanup done"}
    )
    assert post.structured_content["status"] == "held"
    assert slack_server.POSTS == []
    assert [s.name for s in classifier.seen] == ["list_channels", "post_message"]

    action = await svc.store.get(post.structured_content["action_id"])
    assert action is not None and action.approval is not None
    assert action.upstream_server == "slack"
    assert action.reversibility.reason == "not a read verb"
    decision = ApprovalDecisionRequest(
        decision=ApprovalDecision.approve,
        code=action.approval.code,
        channel=ApprovalChannel.dashboard,
        decided_by="dashboard",
    )
    done = await svc.hold_manager.on_decision(action.id, decision)
    assert done.status is ActionStatus.executed
    assert [p["text"] for p in slack_server.POSTS] == ["cleanup done"]


@pytest.mark.integration
async def test_classifier_error_holds_and_rewind_skips_or_rejects(upstream_svc: Any) -> None:
    svc, classifier = upstream_svc
    session = await svc.gateway.start_session(AGENT, SessionMode.live)
    classifier.fail = True
    held = await svc.gateway.call_tool(session, "upstream__slack__list_channels", {})
    assert held.structured_content["status"] == "held"
    action = await svc.store.get(held.structured_content["action_id"])
    assert action is not None and "rules gate failed" in (action.reversibility.reason or "")

    job = await svc.rewinder.create(
        RewindRequest(scope=RewindScope(session_id=session.id), dry_run=False)
    )
    await svc.rewinder.wait()
    done = await svc.store.get_job(job.id)
    assert done is not None and done.steps[0].result is StepResult.skipped
    rejected = await svc.store.get(action.id)
    assert rejected is not None and rejected.status is ActionStatus.rejected


@pytest.mark.integration
async def test_mcp_server_lists_upstream_tools_with_their_schema(upstream_svc: Any) -> None:
    svc, _ = upstream_svc
    mcp, _ = build_mcp(Settings(), svc.gateway)
    async with Client(mcp) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    post = tools["upstream__slack__post_message"]
    assert post.description and post.description.startswith("[via slack; held unless read-only]")
    assert set(post.input_schema["properties"]) == {"channel", "text"}
    assert "pg_delete_rows" not in tools  # no Postgres adapter in this gateway
