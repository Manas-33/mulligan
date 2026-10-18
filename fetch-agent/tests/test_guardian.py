from __future__ import annotations

import json
from typing import Any

import pytest

from mulligan_fetch.guardian_logic import GuardianLogic, parse, rewind_card

SES = "ses_01M561Q6R04CJZXHXRCFFNM7C4"
HELD = {
    "id": "act_01M561R2302EME3JAF7RBZWM16",
    "session_id": SES,
    "system": "email",
    "tool": "email_send",
    "status": "held",
    "summary": 'Email 3 recipients: "Changes"',
    "reversibility": {
        "label": "irreversible",
        "source": "policy",
        "reason": "can't recall an email",
    },
    "approval": {"code": "4821"},
}
DELETE = {
    "id": "act_01M561QDJRQ23HY0VT7QKFVHHZ",
    "session_id": SES,
    "system": "postgres",
    "tool": "pg_delete_rows",
    "status": "executed",
    "summary": "Deleted 30 rows from customers",
    "reversibility": {"label": "reversible", "source": "policy", "reason": None},
}
READ = {
    **DELETE,
    "id": "act_x",
    "tool": "github_list_issues",
    "system": "github",
    "reversibility": {"label": "read_only", "source": "policy", "reason": None},
}


class FakeAPI:
    def __init__(self) -> None:
        self.decisions: list[tuple[str, str, str, str]] = []
        self.rewound: list[str] = []

    async def action(self, action_id: str) -> dict[str, Any]:
        return {**DELETE, "id": action_id}

    async def session_actions(self, session_id: str) -> list[dict[str, Any]]:
        return [READ, DELETE, HELD]

    async def decide(
        self, action_id: str, decision: str, code: str, decided_by: str
    ) -> dict[str, Any]:
        self.decisions.append((action_id, decision, code, decided_by))
        return {**HELD, "status": "approved" if decision == "approve" else "rejected"}

    async def rewind(self, session_id: str, *, dry_run: bool = False) -> dict[str, Any]:
        self.rewound.append(session_id)
        return {
            "id": "rwd_1",
            "status": "completed",
            "steps": [
                {
                    "tool": "email_send",
                    "result": "skipped",
                    "kept_fields": [],
                    "detail": "Rejected while held. Nothing ran.",
                },
                {"result": "kept_human_edit", "kept_fields": ["postgres:products/1/price"]},
                {"result": "restored", "kept_fields": []},
            ],
            "verification": {"postgres": {"ok": True}, "notion": {"ok": True}},
        }


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("@agent1qveg9ca3 rewind", "rewind"),
        ("Undo!", "rewind"),
        ("status", "status"),
        ("", "help"),
        ("hi", "help"),
        ("approve 4821", "approve"),
        ("reject 4821 please", "reject"),
        ("do the quarter-end cleanup", "task"),
        ("approve the budget", "task"),
    ],
)
def test_parse_text(text: str, kind: str) -> None:
    assert parse(text).kind == kind


def test_parse_card_clicks() -> None:
    click = parse(json.dumps({"action": "rewind", "session_id": SES, "approved": True}))
    assert (click.kind, click.session_id) == ("rewind", SES)
    approve = parse(
        json.dumps({"action": "approve", "action_id": "act_1", "code": "4821", "approved": True})
    )
    assert (approve.kind, approve.action_id, approve.code) == ("approve", "act_1", "4821")
    declined = parse(
        json.dumps({"action": "approve", "action_id": "act_1", "code": "4821", "approved": False})
    )
    assert declined.kind == "reject"
    assert parse(json.dumps({"action": "keep", "session_id": SES})).kind == "keep"


def test_rewind_card_uses_string_metadata_and_object_selections() -> None:
    meta = rewind_card(SES, [DELETE, HELD])
    assert all(isinstance(v, str) for v in meta.values())
    assert meta["card_kind"] == "review"
    payload = json.loads(meta["card_payload"])
    assert payload["approve_cta"]["selection"] == {"action": "rewind", "session_id": SES}
    assert payload["reject_cta"]["selection"] == {"action": "keep", "session_id": SES}


async def test_task_report_is_one_reply_with_held_codes_and_a_rewind_card() -> None:
    logic = GuardianLogic(FakeAPI())  # type: ignore[arg-type]
    reply = await logic.task_finished("user1", "cleanup", [], [DELETE["id"]])
    assert logic.last_session["user1"] == SES
    assert "Postgres:" in reply.text and "Deleted 30 rows" in reply.text
    assert "github_list_issues" not in reply.text  # reads are left out
    assert "code 4821" in reply.text and "approve 4821" in reply.text
    assert reply.card and json.loads(reply.card["card_payload"])["title"] == "Rewind this session?"


async def test_task_with_no_actions_says_so() -> None:
    logic = GuardianLogic(FakeAPI())  # type: ignore[arg-type]
    steps = [{"tool": "pg_delete_rows", "status": "skipped"}]
    reply = await logic.task_finished("user1", "cleanup", steps, [])
    assert "couldn't" in reply.text and "pg_delete_rows (skipped)" in reply.text


async def test_rewind_uses_the_last_session_and_reports_kept_edits() -> None:
    api = FakeAPI()
    logic = GuardianLogic(api)  # type: ignore[arg-type]
    none_yet = await logic.handle("user1", parse("rewind"))
    assert "no session" in none_yet.text.lower() and api.rewound == []
    await logic.task_finished("user1", "cleanup", [], [DELETE["id"]])
    reply = await logic.handle("user1", parse("rewind"))
    assert api.rewound == [SES]
    assert "completed" in reply.text and "postgres:products/1/price" in reply.text
    assert "postgres ok" in reply.text and reply.end_session
    assert "Held actions: 1 email rejected, never sent." in reply.text


async def test_approve_by_code_and_by_card_use_the_asi_one_channel() -> None:
    api = FakeAPI()
    logic = GuardianLogic(api)  # type: ignore[arg-type]
    await logic.task_finished("user1", "cleanup", [], [DELETE["id"]])
    reply = await logic.handle("user1", parse("approve 4821"))
    assert api.decisions[-1][:3] == (HELD["id"], "approve", "4821")
    assert api.decisions[-1][3].startswith("asi_one:") and "Approved" in reply.text
    click = json.dumps({"action": "reject", "action_id": HELD["id"], "code": "4821"})
    rejected = await logic.handle("user2", parse(click))
    assert api.decisions[-1][:3] == (HELD["id"], "reject", "4821") and "Rejected" in rejected.text
    missing = await logic.handle("user1", parse("approve 9999"))
    assert "No held action with code 9999" in missing.text


def test_held_outcomes_cover_refunds_and_upstream_calls() -> None:
    from mulligan_fetch.guardian_logic import describe_held_outcomes

    steps = [
        {"tool": "payments_charge", "result": "compensated", "detail": "Refunded"},
        {
            "tool": "upstream__slack__post_message",
            "result": "skipped",
            "detail": "Rejected while held.",
        },
        {"tool": "pg_delete_rows", "result": "restored", "detail": ""},
    ]
    assert (
        describe_held_outcomes(steps) == "1 payment refunded; 1 upstream call rejected, never made"
    )
