"""What the Guardian decides, separate from the uAgents runtime so it can be tested directly.

Chat in ASI:One:
  any task text       -> the worker runs it through Mulligan; the Guardian reports what ran and
                         what's held, with a card to rewind the session and one per held action
  "rewind" / card     -> rewind the user's last protected session and report the result
  "approve 4821"      -> approve (or "reject 4821") a held action from that session
  "status", "help"
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from mulligan_fetch.mulligan_api import MulliganAPI, MulliganError

MENTION = re.compile(r"@agent1[0-9a-z]+")
CODE = re.compile(r"\b(\d{4})\b")

HELP = (
    'I\'m Mulligan Guardian. Give me a task (for example "do the quarter-end cleanup") and my '
    "worker runs it through Mulligan, which records every change and holds anything that can't "
    'be undone. Then say "rewind" to undo the whole session, "approve 1234" or '
    '"reject 1234" for a held action, or "status".'
)


@dataclass
class Reply:
    text: str
    card: dict[str, Any] | None = None  # MetadataContent metadata for an interactive card
    end_session: bool = False


@dataclass
class Command:
    kind: str  # task | rewind | keep | approve | reject | status | help
    text: str = ""
    session_id: str | None = None
    action_id: str | None = None
    code: str | None = None


def parse(raw: str) -> Command:
    text = MENTION.sub("", raw).strip()
    try:
        selection = json.loads(text)
    except ValueError:
        selection = None
    if isinstance(selection, dict) and "action" in selection:
        kind = str(selection["action"])
        if kind in {"approve", "reject"} and selection.get("approved") is False:
            kind = "reject"
        return Command(
            kind=kind if kind in {"rewind", "keep", "approve", "reject"} else "help",
            session_id=selection.get("session_id"),
            action_id=selection.get("action_id"),
            code=selection.get("code"),
        )
    lowered = text.lower().rstrip(".!? ")
    if not lowered or lowered in {"help", "hi", "hello", "hey", "?"}:
        return Command("help")
    if lowered in {"rewind", "undo", "rewind it", "undo it", "rewind the session"}:
        return Command("rewind")
    if lowered in {"status", "what's held", "whats held", "what is held"}:
        return Command("status")
    first = lowered.split()[0]
    if first in {"approve", "reject"} and (match := CODE.search(lowered)):
        return Command(first, code=match.group(1))
    return Command("task", text=text)


def card(kind: str, payload: dict[str, Any]) -> dict[str, str]:
    """MetadataContent for an ASI:One interactive card (every value a string)."""
    return {
        "card_protocol_version": "1",
        "requires_card_interaction": "true",
        "card_kind": kind,
        "card_payload": json.dumps(payload),
    }


def rewind_card(session_id: str, actions: list[dict[str, Any]]) -> dict[str, str]:
    systems = Counter(a["system"] for a in actions)
    held = sum(1 for a in actions if a["status"] == "held")
    rows = [{"label": "Actions", "value": str(len(actions))}]
    rows += [{"label": s.capitalize(), "value": str(n)} for s, n in sorted(systems.items())]
    rows.append({"label": "Held for approval", "value": str(held)})
    return card(
        "review",
        {
            "title": "Rewind this session?",
            "summary_rows": rows,
            "approve_cta": {
                "label": "Rewind",
                "selection": {"action": "rewind", "session_id": session_id},
                "primary": True,
            },
            "reject_cta": {
                "label": "Keep",
                "selection": {"action": "keep", "session_id": session_id},
            },
        },
    )


SYSTEM_NAMES = {"postgres": "Postgres", "github": "GitHub", "notion": "Notion"}


def describe_by_system(actions: list[dict[str, Any]]) -> str:
    """What changed, grouped by system. Reads and held actions are left out."""
    groups: dict[str, list[str]] = {}
    for a in actions:
        if a["reversibility"]["label"] == "read_only" or a["status"] == "held":
            continue
        name = SYSTEM_NAMES.get(a["system"], a["system"].capitalize())
        groups.setdefault(name, []).append(f"- {a.get('summary') or a['tool']} ({a['status']})")
    if not groups:
        return "Nothing changed."
    return "\n\n".join(f"{name}:\n" + "\n".join(lines) for name, lines in groups.items())


def describe_held(action: dict[str, Any]) -> str:
    code = (action.get("approval") or {}).get("code", "?")
    reason = (action.get("reversibility") or {}).get("reason") or action["reversibility"]["label"]
    return f"- {action.get('summary') or action['tool']} (code {code}; {reason})"


def describe_rewind(job: dict[str, Any]) -> str:
    results = Counter(s["result"] for s in job["steps"])
    parts = [f"{n} {r.replace('_', ' ')}" for r, n in sorted(results.items())]
    kept = [f for s in job["steps"] for f in s.get("kept_fields", [])]
    checks = [
        f"{system} {'ok' if v.get('ok') else 'MISMATCH'}"
        for system, v in sorted(job.get("verification", {}).items())
    ]
    text = f"Rewind {job['status']}: {len(job['steps'])} steps ({', '.join(parts) or 'none'})."
    if kept:
        text += "\nKept because a person changed them after the agent: " + ", ".join(kept[:5])
        text += "…" if len(kept) > 5 else ""
    if checks:
        text += "\nChecksums: " + ", ".join(checks) + "."
    return text


@dataclass
class GuardianLogic:
    """Every method returns exactly one Reply: ASI:One shows only the last message per turn."""

    api: MulliganAPI
    last_session: dict[str, str] = field(default_factory=dict)  # chat sender -> session id

    async def task_finished(
        self, sender: str, task: str, steps: list[dict[str, Any]], action_ids: list[str]
    ) -> Reply:
        if not action_ids:
            ran = ", ".join(f"{s['tool']} ({s['status']})" for s in steps) or "nothing"
            return Reply(f"The worker couldn't run that through Mulligan: {ran}.", end_session=True)
        session_id = (await self.api.action(action_ids[0]))["session_id"]
        self.last_session[sender] = session_id
        actions = await self.api.session_actions(session_id)
        held = [a for a in actions if a["status"] == "held"]
        text = f'Done: "{task}" ran through Mulligan (session {session_id}).\n\n'
        text += describe_by_system(actions)
        if held:
            text += "\n\nHeld for your approval (nothing irreversible has happened):\n"
            text += "\n".join(describe_held(a) for a in held)
            code = (held[0].get("approval") or {}).get("code", "1234")
            text += f'\nSay "approve {code}" or "reject {code}".'
        else:
            text += "\n\nNothing is waiting for approval."
        text += '\n\nPress Rewind (or say "rewind") to undo the whole session.'
        return Reply(text, card=rewind_card(session_id, actions))

    async def handle(self, sender: str, command: Command) -> Reply:
        try:
            return await self._handle(sender, command)
        except MulliganError as exc:
            return Reply(f"Mulligan refused that: {exc}", end_session=True)

    async def _handle(self, sender: str, command: Command) -> Reply:
        if command.kind == "help":
            return Reply(HELP, end_session=True)
        session_id = command.session_id or self.last_session.get(sender)
        if command.kind == "keep":
            return Reply("Okay, I'm leaving the session as it is.", end_session=True)
        if session_id is None and command.kind in {"rewind", "status"}:
            return Reply(
                "There's no session to work on yet. Give me a task first.", end_session=True
            )
        if command.kind == "rewind":
            assert session_id is not None
            job = await self.api.rewind(session_id)
            return Reply(describe_rewind(job), end_session=True)
        if command.kind == "status":
            assert session_id is not None
            actions = await self.api.session_actions(session_id)
            return Reply(
                f"Session {session_id}:\n\n{describe_by_system(actions)}", end_session=True
            )
        if command.kind in {"approve", "reject"}:
            return await self._decide(sender, command, session_id)
        raise ValueError(f"not a Guardian command: {command.kind}")

    async def _decide(self, sender: str, command: Command, session_id: str | None) -> Reply:
        action_id, code = command.action_id, command.code
        if action_id is None:
            if session_id is None or code is None:
                return Reply(
                    "Which action? Say approve or reject with its 4-digit code.", end_session=True
                )
            held = [
                a
                for a in await self.api.session_actions(session_id)
                if a["status"] == "held" and (a.get("approval") or {}).get("code") == code
            ]
            if not held:
                return Reply(f"No held action with code {code} in your session.", end_session=True)
            action_id = held[0]["id"]
        decision = "approve" if command.kind == "approve" else "reject"
        action = await self.api.decide(action_id, decision, code or "", f"asi_one:{sender[:24]}")
        verb = "Approved" if decision == "approve" else "Rejected"
        return Reply(
            f"{verb}: {action.get('summary') or action['tool']} (now {action['status']}).",
            end_session=True,
        )
