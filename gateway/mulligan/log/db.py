"""Connection pool and migrations for mulligan-db."""

from __future__ import annotations

from pathlib import Path

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


async def open_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    pool: AsyncConnectionPool = AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    return pool


async def migrate(conn: AsyncConnection, directory: Path = MIGRATIONS) -> list[str]:
    """Apply every `NNN_*.sql` not yet applied, in order. Returns the ones applied now."""
    await conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations"
        " (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    cur = await conn.execute("SELECT version FROM schema_migrations")
    done = {row["version"] if isinstance(row, dict) else row[0] for row in await cur.fetchall()}
    applied = []
    for path in sorted(directory.glob("[0-9][0-9][0-9]_*.sql")):  # noqa: ASYNC240 (startup only)
        if path.stem in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text())  # noqa: ASYNC240
            await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", (path.stem,))
        applied.append(path.stem)
    return applied
