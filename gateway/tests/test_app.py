from __future__ import annotations

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
    # No routes yet, so an authenticated request falls through to 404 instead of 401.
    for headers in ({"Authorization": "Bearer dash-tok"}, {"X-Mulligan-Service-Token": "svc-tok"}):
        assert c.get("/api/sessions", headers=headers).status_code == 404


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
