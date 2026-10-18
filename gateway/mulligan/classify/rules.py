"""The rules gate for upstream tools (MCP servers behind Mulligan, which have no policy entry).

Deterministic, not a model. A tool is ``read_only`` (runs without approval) only if all hold:

1. its name starts with a read verb (``get``, ``list``, ``search``…), in snake, kebab or camel case;
2. its description has no write verb (``send``, ``post``, ``delete``…), in any inflection
   (``deletes``, ``archived``, ``creating``);
3. no other word of its name is a write verb (``get_or_create_user``, ``fetch_and_archive``);
4. it takes no free-form statement (an input named ``sql``, ``mutation``, ``command``…), which
   could write whatever its verb says;
5. its MCP annotations don't say otherwise (``readOnlyHint`` isn't false, ``destructiveHint``
   isn't true).

Anything else is ``irreversible``: Mulligan has no undo adapter for upstream tools, so a write
can't be taken back. The reason names the first rule that failed; the dashboard shows it on the
held action. The gate fails closed: when in doubt, the call waits for a person.
"""

from __future__ import annotations

import re

from mulligan.interfaces import Classifier, UpstreamToolSchema
from mulligan.models import Reversibility, ReversibilityLabel, ReversibilitySource

READ_VERBS = frozenset(
    {
        "get",
        "list",
        "search",
        "read",
        "fetch",
        "describe",
        "find",
        "query",
        "lookup",
        "show",
        "view",
        "count",
    }
)

WRITE_VERBS = frozenset(
    {
        "send", "post", "create", "update", "delete", "remove", "pay", "charge", "invite",
        "publish", "write", "modify", "edit", "set", "add", "insert", "drop", "cancel", "refund",
        "transfer", "archive", "move", "rename", "upload", "notify", "notifies", "notified",
        "share", "approve", "merge", "close", "reopen", "destroy", "revoke", "grant", "ban",
        "kick", "purge", "reset", "overwrite", "replace", "submit", "execute", "run", "trigger",
    }
)  # fmt: skip

# Inputs that take a whole statement: what it does is up to the caller, not the tool's name.
FREE_FORM_INPUTS = frozenset(
    {"sql", "statement", "mutation", "graphql", "command", "cmd", "script", "code", "shell"}
)

_WORD = re.compile(r"[a-z]+")
_SUFFIXES = ("ing", "ed", "es", "s", "d")


def name_words(name: str) -> list[str]:
    """'list_channels' / 'listChannels' / 'ListChannels' -> ['list', 'channels']."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name)
    return _WORD.findall(spaced.lower())


def first_word(name: str) -> str:
    """'list_channels' / 'list-channels' / 'listChannels' / 'ListChannels' -> 'list'."""
    words = name_words(name)
    return words[0] if words else ""


def write_verb(word: str) -> str | None:
    """The write verb `word` is a form of ('archives' -> 'archive', 'running' -> 'run')."""
    if word in WRITE_VERBS:
        return word
    for suffix in _SUFFIXES:
        if not word.endswith(suffix) or len(word) <= len(suffix) + 1:
            continue
        base = word[: -len(suffix)]
        undoubled = base[:-1] if len(base) > 2 and base[-1] == base[-2] else base
        for candidate in (base, base + "e", undoubled):
            if candidate in WRITE_VERBS:
                return candidate
    return None


def write_verbs_in(text: str) -> list[str]:
    return sorted({v for w in _WORD.findall(text.lower()) if (v := write_verb(w))})


def _label(label: ReversibilityLabel, reason: str) -> Reversibility:
    return Reversibility(label=label, source=ReversibilitySource.rules, reason=reason)


class RulesClassifier(Classifier):
    def classify_unknown(self, tool_schema: UpstreamToolSchema) -> Reversibility:
        name = tool_schema.name
        hold = ReversibilityLabel.irreversible

        verb = first_word(name)
        if verb not in READ_VERBS:
            return _label(hold, f"name '{name}' doesn't start with a read verb")

        verbs = write_verbs_in(tool_schema.description or "")
        if verbs:
            return _label(hold, f"description mentions a write ({', '.join(verbs)})")

        in_name = sorted({v for w in name_words(name)[1:] if (v := write_verb(w))})
        if in_name:
            return _label(hold, f"name '{name}' also says {', '.join(in_name)}")

        properties = (tool_schema.input_schema or {}).get("properties") or {}
        free = sorted(k for k in properties if str(k).lower() in FREE_FORM_INPUTS)
        if free:
            return _label(hold, f"it takes a free-form {', '.join(free)}")

        hints = tool_schema.annotations
        if hints is not None and hints.readOnlyHint is False:
            return _label(hold, "the server marks it as not read-only")
        if hints is not None and hints.destructiveHint is True:
            return _label(hold, "the server marks it as destructive")

        return _label(
            ReversibilityLabel.read_only, f"'{verb}' tool with no write in its description"
        )
