"""Settings for the Guardian and worker, read from the environment and the repo's .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> dict[str, str]:
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


def agent_token(spec: str, agent_id: str) -> str:
    """The token for `agent_id` in MULLIGAN_AGENT_TOKENS (`id:token,id:token`)."""
    for item in spec.split(","):
        name, _, token = item.strip().partition(":")
        if name == agent_id and token:
            return token
    return ""


@dataclass(frozen=True)
class Config:
    api_url: str
    mcp_url: str
    service_token: str
    fetch_token: str
    guardian_seed: str
    worker_seed: str
    asi_one_api_key: str
    guardian_port: int
    worker_port: int

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Config:
        dotenv = Path(os.environ.get("MULLIGAN_DOTENV", str(REPO_ROOT / ".env")))
        env = {**load_dotenv(dotenv), **(environ if environ is not None else os.environ)}

        def get(key: str, default: str = "") -> str:
            return env.get(key, "").strip() or default

        api_port = get("MULLIGAN_API_PORT", "8000")
        mcp_port = get("MULLIGAN_MCP_PORT", "8001")
        return cls(
            api_url=get("MULLIGAN_API_URL", f"http://127.0.0.1:{api_port}").rstrip("/"),
            mcp_url=get("MULLIGAN_MCP_URL", f"http://127.0.0.1:{mcp_port}/mcp"),
            service_token=get("MULLIGAN_SERVICE_TOKEN"),
            fetch_token=agent_token(get("MULLIGAN_AGENT_TOKENS"), "fetch"),
            guardian_seed=get("GUARDIAN_AGENT_SEED"),
            worker_seed=get("WORKER_AGENT_SEED"),
            asi_one_api_key=get("ASI_ONE_API_KEY"),
            guardian_port=int(get("GUARDIAN_AGENT_PORT", "8010")),
            worker_port=int(get("WORKER_AGENT_PORT", "8011")),
        )
