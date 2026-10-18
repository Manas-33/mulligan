"""A teammate fixes things by hand after the rogue agent ran, straight in the systems, not through
Mulligan. Two edits touch something the agent also changed, so the rewind keeps and flags them;
one touches something the agent never changed.

  uv --directory gateway run python ../demo/teammate_edit.py

1. Postgres: products WID-001 price -> 45.00 (the agent set it to 0; kept and flagged)
2. Notion: the pricing table's Widget Basic row -> $15 (the agent set $0; kept and flagged)
3. Postgres: support ticket 1's subject (the agent never touched it; the rewind leaves it alone)
"""

from __future__ import annotations

import asyncio
import os

import psycopg

from mulligan.adapters.notion.adapter import NotionAdapter
from mulligan.adapters.notion.demo import teammate_edit as notion_teammate_edit
from mulligan.settings import REPO_ROOT, Settings, load_dotenv

for _key, _value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(_key, _value)

ADMIN_URL = os.environ.get("PROD_DB_ADMIN_URL", "postgresql://acme:acme@127.0.0.1:5434/acme")


def postgres() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        cur = conn.execute("update products set price = 45.00 where sku = 'WID-001' returning name")
        product = cur.fetchone()
        print(f"postgres: {product[0] if product else 'WID-001 (missing)'} price -> 45.00")
        conn.execute(
            "update support_tickets set subject = 'Late delivery (called customer back)'"
            " where id = 1"
        )
        print("postgres: support ticket 1 subject updated")


async def notion() -> None:
    settings = Settings.from_env()
    if not settings.notion_token:
        print("notion: skipped (NOTION_TOKEN is not set)")
        return
    adapter = await NotionAdapter.open(settings)
    try:
        print(await notion_teammate_edit(adapter))
    finally:
        await adapter.close()


def main() -> None:
    postgres()
    asyncio.run(notion())


if __name__ == "__main__":
    main()
