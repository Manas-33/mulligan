from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mulligan.app import create_app
from mulligan.models import AgentKind, SessionMode
from mulligan.settings import Settings, parse_agent_tokens

SETTINGS = Settings(dashboard_token="dash-tok", service_token="svc-tok")


def client() -> TestClient:
    return TestClient(create_app(SETTINGS))


def test_healthz_needs_no_token() -> None:
    assert client().get("/healthz").json() == {"ok": True}


def test_api_rejects_missing_or_wrong_token() -> None:
    c = client()
    assert c.get("/api/sessions").status_code == 401
    assert c.get("/api/sessions", headers={"Authorization": "Bearer nope"}).status_code == 401
    wrong_header = {"X-Mulligan-Service-Token": "dash-tok"}
    assert c.get("/api/sessions", headers=wrong_header).status_code == 401


def test_api_accepts_either_token() -> None:
    c = client()
    # An unknown path behind a good token is a 404, not a 401.
    for headers in ({"Authorization": "Bearer dash-tok"}, {"X-Mulligan-Service-Token": "svc-tok"}):
        assert c.get("/api/nope", headers=headers).status_code == 404


def test_unconfigured_tokens_never_match() -> None:
    c = TestClient(create_app(Settings()))
    assert c.get("/api/x", headers={"Authorization": "Bearer "}).status_code == 401
    assert c.get("/api/x", headers={"X-Mulligan-Service-Token": ""}).status_code == 401


def test_agent_tokens() -> None:
    tokens = parse_agent_tokens("claude_code:t1, devin:t2,rogue_replay:t4,my_bot:t5")
    assert tokens["t1"].agent.kind is AgentKind.claude_code
    assert tokens["t1"].mode is SessionMode.live
    assert tokens["t4"].agent.kind is AgentKind.rogue
    assert tokens["t4"].mode is SessionMode.replay
    assert tokens["t5"].agent.kind is AgentKind.other
    assert tokens["t5"].agent.display_name == "my_bot"


def test_cors_preflight_passes_and_401_carries_cors_headers() -> None:
    c = client()
    origin = {"Origin": "http://localhost:5173"}
    pre = c.options(
        "/api/sessions",
        headers={
            **origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert pre.status_code == 200
    denied = c.get("/api/sessions", headers=origin)
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_payment_hold_must_capture_before_the_authorization_lapses() -> None:
    import pytest

    from mulligan.log.store import HoldWindowError, check_hold_window
    from mulligan.models import Action

    base = {
        "id": "act_01M561R8XR9B8FKFRNVHR3M4C9",
        "session_id": "ses_01M561Q6R04CJZXHXRCFFNM7C4",
        "agent": {"id": "c", "kind": "claude_code", "display_name": "C"},
        "system": "payments",
        "tool": "payments_charge",
        "upstream_server": None,
        "args_redacted": {},
        "status": "executed",
        "reversibility": {"label": "compensable", "source": "policy", "reason": None},
        "summary": None,
        "error": None,
        "before_ref": None,
        "after_ref": None,
        "after_hashes": {},
        "inverse": None,
        "approval": None,
        "explanation": None,
        "sentry_trace_id": None,
        "created_at": "2026-10-17T23:00:00Z",
        "executed_at": None,
        "reverted_at": None,
    }
    hold = {"kind": "payment_auth", "external_id": "pi_1", "release_at": "2026-10-18T00:00:00Z"}
    ok = Action.model_validate(
        {**base, "hold": {**hold, "authorization_expires_at": "2026-10-24T00:00:00Z"}}
    )
    check_hold_window(ok)
    late = Action.model_validate(
        {**base, "hold": {**hold, "authorization_expires_at": "2026-10-17T23:30:00Z"}}
    )
    with pytest.raises(HoldWindowError):
        check_hold_window(late)


def test_a_comment_where_a_value_should_be_counts_as_unset(tmp_path: Path) -> None:
    from mulligan.settings import load_dotenv

    env_file = tmp_path / ".env"
    env_file.write_text(
        "UPSTREAM_MCP_SERVERS=   # e.g. slack=http://x/mcp\nMULLIGAN_DEMO=1  # on\n"
    )
    assert load_dotenv(env_file) == {"UPSTREAM_MCP_SERVERS": "", "MULLIGAN_DEMO": "1"}
    settings = Settings.from_env(
        {
            "UPSTREAM_MCP_SERVERS": "# e.g. slack",
            "MULLIGAN_MCP_PUBLIC_URL": "#",
            "MULLIGAN_DEMO": "1",
        }
    )
    assert settings.upstream_mcp_servers == ""
    assert settings.mcp_public_url == ""
    assert settings.demo is True


def test_sentry_sends_info_logs_and_traces() -> None:
    import logging

    from sentry_sdk.integrations.logging import LoggingIntegration

    from mulligan.__main__ import sentry_options

    options = sentry_options(Settings(sentry_dsn="https://key@o0.ingest.sentry.io/0"))
    assert options["enable_logs"] is True and options["traces_sample_rate"] == 1.0
    [integration] = options["integrations"]
    assert isinstance(integration, LoggingIntegration)
    assert integration._sentry_logs_handler.level == logging.INFO
