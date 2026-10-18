"""Stream B's rules gate (mulligan.classify) for upstream tools."""

from __future__ import annotations

import pytest

from mulligan import services
from mulligan.classify import RulesClassifier, create_classifier
from mulligan.classify.rules import first_word
from mulligan.interfaces import ToolAnnotations, UpstreamToolSchema
from mulligan.models import ReversibilityLabel, ReversibilitySource

gate = RulesClassifier()


def tool(
    name: str, description: str | None = None, annotations: ToolAnnotations | None = None
) -> UpstreamToolSchema:
    return UpstreamToolSchema(
        server="slack",
        name=name,
        exposed_name=f"upstream__slack__{name}",
        description=description,
        annotations=annotations,
    )


def label(t: UpstreamToolSchema) -> ReversibilityLabel:
    return gate.classify_unknown(t).label


def test_mock_slack_tools() -> None:
    read = gate.classify_unknown(
        tool("list_channels", "List the workspace's channels.", ToolAnnotations(readOnlyHint=True))
    )
    post = gate.classify_unknown(
        tool(
            "post_message",
            "Post a message to a channel. Everyone in the channel is notified.",
            ToolAnnotations(readOnlyHint=False),
        )
    )
    assert read.label is ReversibilityLabel.read_only
    assert read.source is ReversibilitySource.rules
    assert post.label is ReversibilityLabel.irreversible
    assert post.reason == "name 'post_message' doesn't start with a read verb"


@pytest.mark.parametrize(
    ("name", "word"),
    [
        ("list_channels", "list"),
        ("list-channels", "list"),
        ("listChannels", "list"),
        ("ListChannels", "list"),
        ("getUserByID", "get"),
        ("_search", "search"),
        ("", ""),
    ],
)
def test_first_word(name: str, word: str) -> None:
    assert first_word(name) == word


@pytest.mark.parametrize("name", ["getUser", "search_issues", "fetch-page", "describe_table"])
def test_read_verbs_without_writes_run(name: str) -> None:
    assert label(tool(name, "Returns details.")) is ReversibilityLabel.read_only


@pytest.mark.parametrize(
    ("t", "reason_part"),
    [
        (tool("send_email", "Send an email."), "doesn't start with a read verb"),
        (tool("pg_query"), "doesn't start with a read verb"),
        (tool("get_and_delete", "Fetch a row, then delete it."), "description mentions a write"),
        (tool("list_and_notify", "Lists members and notifies them."), "description mentions"),
        (tool("list_items", "List.", ToolAnnotations(readOnlyHint=False)), "not read-only"),
        (tool("get_item", "Get.", ToolAnnotations(destructiveHint=True)), "destructive"),
        (tool("listing", "Returns listings."), "doesn't start with a read verb"),
    ],
)
def test_anything_else_is_held(t: UpstreamToolSchema, reason_part: str) -> None:
    result = gate.classify_unknown(t)
    assert result.label is ReversibilityLabel.irreversible
    assert result.reason is not None and reason_part in result.reason


def test_core_picks_up_the_rules_gate() -> None:
    assert isinstance(create_classifier(), RulesClassifier)
    assert isinstance(services.make_classifier(), RulesClassifier)
