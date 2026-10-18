"""Checks that the contracts agree with each other and with the generated models."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema
import pytest
import yaml

from mulligan import models
from mulligan.policy import YamlPolicy

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
SCHEMA = json.loads((CONTRACTS / "action.schema.json").read_text())
OPENAPI = yaml.safe_load((CONTRACTS / "openapi.yaml").read_text())


def test_schema_is_valid_2020_12() -> None:
    jsonschema.Draft202012Validator.check_schema(SCHEMA)


def _tools_in_mcp_tools_md() -> set[str]:
    text = (CONTRACTS / "mcp_tools.md").read_text()
    return set(re.findall(r"^\| `([a-z_]+)` \|", text, flags=re.MULTILINE))


def test_policy_covers_exactly_the_documented_tools() -> None:
    policy = YamlPolicy.load(CONTRACTS / "reversibility_policy.yaml")
    documented = _tools_in_mcp_tools_md()
    assert len(documented) == 15
    assert policy.tools() == documented


def test_only_read_tools_are_read_only() -> None:
    policy = YamlPolicy.load(CONTRACTS / "reversibility_policy.yaml")
    read_only = {t for t in policy.tools() if policy.lookup(t).label == "read_only"}  # type: ignore[union-attr]
    assert read_only == {"pg_query", "github_list_issues", "notion_get_page", "mulligan_status"}
    assert policy.hold_kind("email_send") == "email_outbox"
    assert policy.hold_kind("payments_charge") == "payment_auth"
    assert policy.lookup("upstream__slack__post_message") is None


_EXAMPLE_MODELS = {
    "Session": models.Session,
    "Action": models.Action,
    "Rewind": models.RewindJob,
    "Checksums": None,
}


@pytest.mark.parametrize("name", sorted(OPENAPI["components"]["examples"]))
def test_openapi_examples_parse_as_models(name: str) -> None:
    value = OPENAPI["components"]["examples"][name]["value"]
    model = next((m for prefix, m in _EXAMPLE_MODELS.items() if name.startswith(prefix)), None)
    if model is None:
        for system, entry in value.items():
            models.System(system)
            models.SystemChecksum.model_validate(entry)
        return
    parsed = model.model_validate(value)
    assert parsed.model_dump(mode="json", exclude_none=False)["id"] == value["id"]


def test_event_envelope_narrows_on_event_name() -> None:
    action = OPENAPI["components"]["examples"]["ActionEmailHeld"]["value"]
    event = models.MulliganEvent.model_validate(
        {"event": "action.held", "seq": 41, "ts": "2026-10-17T23:04:28Z", "data": action}
    )
    assert isinstance(event.root, models.ActionEvent)
    with pytest.raises(ValueError):
        models.MulliganEvent.model_validate(
            {"event": "session.started", "seq": 1, "ts": "2026-10-17T23:04:28Z", "data": action}
        )


@pytest.mark.parametrize("handle", ["+15555550123", "approver@icloud.com"])
def test_approver_handle_is_a_phone_or_an_email(handle: str) -> None:
    models.NotifyApprovalRequest(
        action_id="act_01M561R2302EME3JAF7RBZWM16", code="4821", summary="x", to=handle
    )


@pytest.mark.parametrize("handle", ["5555550123", "not an email", "@icloud.com"])
def test_approver_handle_rejects_anything_else(handle: str) -> None:
    with pytest.raises(ValueError):
        models.NotifySummaryRequest(
            rewind_job_id="rwd_01M561RFRGVASABYV8XG40DDX0", text="x", to=handle
        )


@pytest.mark.parametrize("value", [49, "Classic Widget", None, True, [1, 2], {"id": 7}])
def test_field_change_values_are_any_json(value: object) -> None:
    change = models.FieldChange(
        field="postgres:products/42/price",
        before=value,
        agent_value=value,
        current=value,
        outcome=models.FieldChangeOutcome.restore,
    )
    assert change.model_dump(mode="json")["before"] == value


def test_payment_hold_carries_its_authorization_expiry() -> None:
    hold = models.Hold.model_validate(
        {
            "kind": "payment_auth",
            "release_at": "2026-10-18T00:00:00Z",
            "authorization_expires_at": "2026-10-24T00:00:00Z",
            "external_id": "pi_123",
        }
    )
    assert hold.release_at is not None and hold.authorization_expires_at is not None
    assert hold.release_at < hold.authorization_expires_at
