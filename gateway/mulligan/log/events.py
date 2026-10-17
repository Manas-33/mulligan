"""The event bus: stores every event in mulligan-db and fans it out to SSE subscribers."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from mulligan.models import EventName, MulliganEvent

REPLAY_LIMIT = 1000
QUEUE_SIZE = 1000


class EventBus:
    """Implements `interfaces.EventBus`."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool
        self._subscribers: set[asyncio.Queue[MulliganEvent | None]] = set()
        # Insert and fan-out happen under one lock so subscribers see seq in order.
        self._lock = asyncio.Lock()

    async def publish(self, event: EventName | str, data: BaseModel) -> int:
        name = EventName(event).value
        payload = data.model_dump(mode="json")
        async with self._lock:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    "INSERT INTO events (ts, event, data) VALUES (now(), %s, %s) RETURNING seq, ts",
                    (name, Jsonb(payload)),
                )
                row: Any = await cur.fetchone()
            envelope = MulliganEvent.model_validate(
                {"event": name, "seq": row["seq"], "ts": row["ts"], "data": payload}
            )
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    # A subscriber that can't keep up is dropped; it reconnects with Last-Event-ID.
                    self._subscribers.discard(queue)
                    with contextlib.suppress(asyncio.QueueEmpty):
                        queue.get_nowait()
                    queue.put_nowait(None)
        return int(row["seq"])

    async def replay(self, after_seq: int) -> list[MulliganEvent]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT seq, ts, event, data FROM (
                    SELECT * FROM events WHERE seq > %s ORDER BY seq DESC LIMIT %s
                ) recent ORDER BY seq
                """,
                (after_seq, REPLAY_LIMIT),
            )
            rows: list[Any] = await cur.fetchall()
        return [
            MulliganEvent.model_validate(
                {"event": r["event"], "seq": r["seq"], "ts": r["ts"], "data": r["data"]}
            )
            for r in rows
        ]

    async def subscribe(self, last_seq: int | None = None) -> AsyncIterator[MulliganEvent]:
        """Live events, preceded by everything after `last_seq` when it's given."""
        queue: asyncio.Queue[MulliganEvent | None] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self._subscribers.add(queue)
        try:
            seen = last_seq or 0
            if last_seq is not None:
                for envelope in await self.replay(last_seq):
                    seen = envelope.root.seq
                    yield envelope
            while True:
                live = await queue.get()
                if live is None:
                    return
                if live.root.seq <= seen:
                    continue
                seen = live.root.seq
                yield live
        finally:
            self._subscribers.discard(queue)
