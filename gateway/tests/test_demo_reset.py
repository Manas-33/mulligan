"""POST /api/demo/reset tells open dashboards to drop what they show and reload."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from mulligan.api.routes import demo_reset
from mulligan.models import DemoResetResult, EventName

NOW = datetime(2026, 10, 18, tzinfo=UTC)


async def test_reset_publishes_demo_reset_after_clearing_the_log() -> None:
    calls: list[Any] = []

    async def reseed() -> None:
        calls.append("reseed")

    async def clear() -> None:
        calls.append("clear")

    async def checksums(recompute: bool) -> dict[str, tuple[str, datetime]]:
        return {"postgres": ("abc", NOW)}

    async def publish(event: EventName, data: BaseModel) -> int:
        calls.append((event, data))
        return 1

    svc = SimpleNamespace(
        settings=SimpleNamespace(demo=True),
        rewinder=SimpleNamespace(running=None),
        demo_resets=[reseed],
        store=SimpleNamespace(clear=clear),
        current_checksums=checksums,
        bus=SimpleNamespace(publish=publish),
    )
    result = await demo_reset(svc)  # type: ignore[arg-type]
    assert calls == ["reseed", "clear", (EventName.demo_reset, result)]
    assert isinstance(result, DemoResetResult) and result.checksums["postgres"].hash == "abc"
