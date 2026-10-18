"""`python -m mulligan`: the gateway. Two servers in one process sharing one set of services:
REST + SSE on MULLIGAN_API_PORT and MCP on MULLIGAN_MCP_PORT, both on MULLIGAN_BIND_HOST
(127.0.0.1 unless compose says otherwise)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import sentry_sdk
import uvicorn
from sentry_sdk.integrations.logging import LoggingIntegration

from mulligan.adapters.github.adapter import GitHubAdapter
from mulligan.adapters.notion.adapter import NotionAdapter
from mulligan.adapters.postgres.adapter import PostgresAdapter
from mulligan.api.auth import HEALTH_PATHS
from mulligan.app import create_app
from mulligan.interfaces import Adapter
from mulligan.mcp_server.server import create_mcp_app
from mulligan.services import build_services
from mulligan.settings import Settings
from mulligan.upstream import UpstreamAdapter, parse_servers

log = logging.getLogger("mulligan")


class SkipHealthChecks(logging.Filter):
    """Drops uvicorn's access line for GET /healthz. Docker's healthcheck calls it every few
    seconds, which would bury the gateway's own lines in stdout and in Sentry Logs (a filter on
    the logger runs before both)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 3:
            return True
        return str(args[2]).split("?", 1)[0] not in HEALTH_PATHS


async def serve(settings: Settings) -> None:
    adapters: list[Adapter] = []
    github = await GitHubAdapter.open(settings) if settings.github_token else None
    if github:
        adapters.append(github)
    postgres = await PostgresAdapter.open(settings) if settings.prod_db_url else None
    if postgres:
        adapters.append(postgres)
    notion = await NotionAdapter.open(settings) if settings.notion_token else None
    if notion:
        adapters.append(notion)
    upstream = UpstreamAdapter(parse_servers(settings.upstream_mcp_servers))
    if upstream.servers:
        await upstream.discover()
        adapters.append(upstream)
        log.info("upstream tools: %s", ", ".join(sorted(upstream.schemas)) or "none")
    services = await build_services(settings, adapters)
    services.gateway.upstream.update(upstream.schemas)
    if postgres and settings.demo and settings.prod_db_admin_url:
        services.demo_resets.append(postgres.reset)
    if notion and settings.demo:
        services.demo_resets.append(notion.reset)
    services.start_background()
    rest = create_app(settings, services=services)
    mcp = create_mcp_app(settings, services.gateway)
    if github and settings.demo:
        services.demo_resets.append(github.reset)
    servers = [
        uvicorn.Server(uvicorn.Config(rest, host=settings.bind_host, port=settings.api_port)),
        uvicorn.Server(uvicorn.Config(mcp, host=settings.bind_host, port=settings.mcp_port)),
    ]
    # After the configs: each one re-applies uvicorn's logging config.
    logging.getLogger("uvicorn.access").addFilter(SkipHealthChecks())
    log.info(
        "REST on %s:%s, MCP on %s:%s/mcp",
        settings.bind_host,
        settings.api_port,
        settings.bind_host,
        settings.mcp_port,
    )
    try:
        await asyncio.gather(*(s.serve() for s in servers))
    finally:
        await services.close()
        if postgres:
            await postgres.close()
        if github:
            await github.close()
        if notion:
            await notion.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = Settings.from_env()
    if settings.sentry_dsn:
        sentry_sdk.init(**sentry_options(settings))
    asyncio.run(serve(settings))


def sentry_options(settings: Settings) -> dict[str, Any]:
    """Tracing for every action and rewind, plus Sentry Logs: the gateway's stdlib logging at
    INFO and above goes to Sentry as logs (errors still become events)."""
    return {
        "dsn": settings.sentry_dsn,
        "environment": settings.sentry_environment,
        "traces_sample_rate": 1.0,
        "send_default_pii": False,
        "enable_logs": True,
        "integrations": [
            LoggingIntegration(
                level=logging.INFO, event_level=logging.ERROR, sentry_logs_level=logging.INFO
            )
        ],
    }


if __name__ == "__main__":
    main()
