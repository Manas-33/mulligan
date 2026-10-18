"""Builds the gateway's parts and plugs in Stream B's when they exist."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from mulligan.core import Gateway, utcnow
from mulligan.interfaces import Adapter, Classifier, HoldDeps, HoldManager
from mulligan.log.db import migrate, open_pool
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.orchestrator.rewind import Rewinder
from mulligan.policy import YamlPolicy
from mulligan.settings import Settings
from mulligan.status import StatusAdapter
from mulligan.stubs import BasicHoldManager, HoldEverythingClassifier

log = logging.getLogger("mulligan")

RELEASE_INTERVAL_S = 5.0


def _plugin(module: str, name: str) -> Callable[..., Any] | None:
    """`mulligan.<module>.<name>` if Stream B has shipped it, else None."""
    try:
        mod = importlib.import_module(f"mulligan.{module}")
    except ImportError:
        return None
    factory = getattr(mod, name, None)
    return factory if callable(factory) else None


def make_classifier() -> Classifier:
    factory = _plugin("classify", "create_classifier")
    return factory() if factory else HoldEverythingClassifier()


def make_hold_adapters() -> list[Adapter]:
    factory = _plugin("holds", "create_adapters")
    return list(factory()) if factory else []


def make_hold_manager(deps: HoldDeps) -> HoldManager:
    factory = _plugin("holds", "create_hold_manager")
    return factory(deps) if factory else BasicHoldManager(deps)


@dataclass
class Services:
    settings: Settings
    store: LogStore
    bus: EventBus
    gateway: Gateway
    rewinder: Rewinder
    pool: AsyncConnectionPool | None = None
    checksums: dict[str, tuple[str, datetime]] = field(default_factory=dict)
    demo_resets: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    """Run by POST /api/demo/reset before the log is cleared (adapters reseed their systems)."""
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def hold_manager(self) -> HoldManager:
        assert self.gateway.hold_manager is not None
        return self.gateway.hold_manager

    async def current_checksums(self, recompute: bool) -> dict[str, tuple[str, datetime]]:
        if recompute or not self.checksums:
            for adapter in self.gateway.adapters:
                value = await adapter.checksum()
                if value is not None:
                    self.checksums[adapter.system.value] = (value, utcnow())
        return dict(self.checksums)

    def start_background(self) -> None:
        self._tasks.append(asyncio.create_task(self._release_loop()))

    async def _release_loop(self) -> None:
        while True:
            await asyncio.sleep(RELEASE_INTERVAL_S)
            try:
                await self.hold_manager.release_due()
            except Exception:  # noqa: BLE001 (keep the loop alive; Sentry gets it)
                log.exception("release_due failed")

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.rewinder.wait()
        if self.pool is not None:
            await self.pool.close()


def assemble(
    settings: Settings,
    store: LogStore,
    bus: EventBus,
    adapters: list[Adapter],
    *,
    classifier: Classifier | None = None,
    pool: AsyncConnectionPool | None = None,
) -> Services:
    gateway = Gateway(
        store=store,
        bus=bus,
        policy=YamlPolicy.load(settings.policy_path),
        classifier=classifier or make_classifier(),
        adapters=[*adapters, StatusAdapter(store), *make_hold_adapters()],
    )
    gateway.hold_manager = make_hold_manager(HoldDeps(store=store, bus=bus, runner=gateway))
    return Services(
        settings=settings,
        store=store,
        bus=bus,
        gateway=gateway,
        rewinder=Rewinder(gateway),
        pool=pool,
    )


async def build_services(settings: Settings, adapters: list[Adapter]) -> Services:
    pool = await open_pool(settings.mulligan_db_url)
    async with pool.connection() as conn:
        applied = await migrate(conn)
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    store = LogStore(pool)
    return assemble(settings, store, EventBus(pool), adapters, pool=pool)
