"""Gateway settings, read once from the environment (and `.env` on the host)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from mulligan.models import Agent, AgentKind, SessionMode

REPO_ROOT = Path(__file__).resolve().parents[2]

_AGENT_DISPLAY = {
    AgentKind.claude_code: "Claude Code",
    AgentKind.devin: "Devin",
    AgentKind.fetch: "Fetch Guardian",
    AgentKind.rogue: "Cleanup agent",
    AgentKind.other: "Agent",
}


@dataclass(frozen=True)
class AgentToken:
    agent: Agent
    mode: SessionMode


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal `.env` reader: KEY=VALUE lines, `#` comments, optional quotes."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in {'"', "'"} and value.endswith(value[0]) and len(value) > 1:
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key.strip()] = value
    return values


def parse_agent_tokens(spec: str) -> dict[str, AgentToken]:
    """`claude_code:tok1,devin:tok2,rogue_replay:tok4` -> token -> agent.

    The id before the colon picks the agent kind (`rogue_replay` is kind `rogue`). An id ending in
    `_replay` marks its sessions `mode: replay`.
    """
    tokens: dict[str, AgentToken] = {}
    for item in filter(None, (part.strip() for part in spec.split(","))):
        agent_id, sep, token = item.partition(":")
        if not sep or not agent_id or not token:
            raise ValueError("MULLIGAN_AGENT_TOKENS entries look like <agent_id>:<token>")
        replay = agent_id.endswith("_replay")
        base = agent_id.removesuffix("_replay")
        kind = AgentKind(base) if base in AgentKind.__members__ else AgentKind.other
        display = _AGENT_DISPLAY[kind] + (" (replay)" if replay else "")
        if kind is AgentKind.other:
            display = agent_id
        tokens[token] = AgentToken(
            agent=Agent(id=agent_id, kind=kind, display_name=display),
            mode=SessionMode.replay if replay else SessionMode.live,
        )
    return tokens


@dataclass(frozen=True)
class Settings:
    env: str = "dev"
    demo: bool = False
    bind_host: str = "127.0.0.1"
    api_port: int = 8000
    mcp_port: int = 8001
    mcp_public_url: str = ""
    dashboard_token: str = ""
    service_token: str = ""
    agent_tokens: Mapping[str, AgentToken] = field(default_factory=dict)
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")
    mulligan_db_url: str = ""
    prod_db_url: str = ""
    prod_db_readonly_url: str = ""
    pg_query_timeout_ms: int = 5000
    pg_query_max_rows: int = 1000
    pg_allowed_schemas: tuple[str, ...] = ("public",)
    policy_path: Path = REPO_ROOT / "contracts" / "reversibility_policy.yaml"
    sentry_dsn: str = ""
    sentry_environment: str = "dev"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        env = {**load_dotenv(REPO_ROOT / ".env"), **(environ or os.environ)}

        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        def csv(key: str, default: str) -> tuple[str, ...]:
            return tuple(p.strip() for p in get(key, default).split(",") if p.strip())

        return cls(
            env=get("MULLIGAN_ENV", "dev"),
            demo=get("MULLIGAN_DEMO", "0") == "1",
            bind_host=get("MULLIGAN_BIND_HOST", "127.0.0.1"),
            api_port=int(get("MULLIGAN_API_PORT", "8000")),
            mcp_port=int(get("MULLIGAN_MCP_PORT", "8001")),
            mcp_public_url=get("MULLIGAN_MCP_PUBLIC_URL"),
            dashboard_token=get("MULLIGAN_DASHBOARD_TOKEN"),
            service_token=get("MULLIGAN_SERVICE_TOKEN"),
            agent_tokens=parse_agent_tokens(get("MULLIGAN_AGENT_TOKENS")),
            cors_origins=csv("MULLIGAN_CORS_ORIGINS", ",".join(cls.cors_origins)),
            mulligan_db_url=get("MULLIGAN_DB_URL"),
            prod_db_url=get("PROD_DB_URL"),
            prod_db_readonly_url=get("PROD_DB_READONLY_URL"),
            pg_query_timeout_ms=int(get("PG_QUERY_TIMEOUT_MS", "5000")),
            pg_query_max_rows=int(get("PG_QUERY_MAX_ROWS", "1000")),
            pg_allowed_schemas=csv("PG_ALLOWED_SCHEMAS", "public"),
            policy_path=Path(get("MULLIGAN_POLICY_PATH", str(cls.policy_path))),
            sentry_dsn=get("SENTRY_DSN_GATEWAY"),
            sentry_environment=get("SENTRY_ENVIRONMENT", "dev"),
        )
