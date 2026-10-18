"""Stream B's rules gate (mulligan.classify) for upstream tools."""

from __future__ import annotations

import pytest

from mulligan import services
from mulligan.classify import RulesClassifier, create_classifier
from mulligan.classify.rules import first_word, write_verb
from mulligan.interfaces import ToolAnnotations, UpstreamToolSchema
from mulligan.models import ReversibilityLabel, ReversibilitySource

gate = RulesClassifier()


def tool(
    name: str,
    description: str | None = None,
    annotations: ToolAnnotations | None = None,
    inputs: tuple[str, ...] = (),
) -> UpstreamToolSchema:
    return UpstreamToolSchema(
        server="slack",
        name=name,
        exposed_name=f"upstream__slack__{name}",
        description=description,
        annotations=annotations,
        input_schema={"type": "object", "properties": {k: {"type": "string"} for k in inputs}},
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


@pytest.mark.parametrize(
    "name", ["getUser", "search_issues", "fetch-page", "describe_table", "get_user_settings"]
)
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
        (tool("fetch_and_archive", "Fetches mail."), "also says archive"),
        (tool("get_or_create_user", "Returns a user."), "also says create"),
        (tool("lookup", "Deletes stale keys."), "description mentions a write (delete)"),
        (tool("find", "Finds rows; archived ones are purged."), "archive, purge"),
        (tool("query_db", "Runs it.", inputs=("sql",)), "description mentions a write (run)"),
        (tool("query_db", "Returns rows.", inputs=("sql",)), "free-form sql"),
        (tool("describe", "Returns rows.", inputs=("Mutation",)), "free-form Mutation"),
    ],
)
def test_anything_else_is_held(t: UpstreamToolSchema, reason_part: str) -> None:
    result = gate.classify_unknown(t)
    assert result.label is ReversibilityLabel.irreversible
    assert result.reason is not None and reason_part in result.reason


def test_core_picks_up_the_rules_gate() -> None:
    assert isinstance(create_classifier(), RulesClassifier)
    assert isinstance(services.make_classifier(), RulesClassifier)


@pytest.mark.parametrize(
    ("word", "verb"),
    [
        ("archives", "archive"),
        ("deleted", "delete"),
        ("creating", "create"),
        ("running", "run"),
        ("submitted", "submit"),
        ("posts", "post"),
        ("settings", None),
        ("address", None),
        ("channels", None),
    ],
)
def test_write_verb_inflections(word: str, verb: str | None) -> None:
    assert write_verb(word) == verb
