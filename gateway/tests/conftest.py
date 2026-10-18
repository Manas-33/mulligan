from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import psycopg
import pytest
from psycopg import sql

from mulligan.log.db import migrate, open_pool

ADMIN_URL = os.environ.get(
    "MULLIGAN_TEST_DB_URL", "postgresql://mulligan:mulligan@127.0.0.1:5433/mulligan"
)


@pytest.fixture
async def log_db_url() -> AsyncIterator[str]:
    """A fresh, migrated database on mulligan-db, dropped afterwards (needs `make db`)."""
    name = f"mulligan_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError as exc:
        pytest.skip(f"mulligan-db not reachable ({exc}); run `make db`")
    async with admin:
        await admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        await migrate(conn)
    try:
        yield url
    finally:
        async with await psycopg.AsyncConnection.connect(ADMIN_URL, autocommit=True) as admin:
            await admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
async def log_pool(log_db_url: str):  # type: ignore[no-untyped-def]
    pool = await open_pool(log_db_url, max_size=5)
    try:
        yield pool
    finally:
        await pool.close()
