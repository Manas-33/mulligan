"""Demo helpers for the Notion pricing page, outside the gateway.

  uv --directory gateway run python -m mulligan.adapters.notion.demo teammate  # fix one price
  uv --directory gateway run python -m mulligan.adapters.notion.demo reset     # back to the seed
  uv --directory gateway run python -m mulligan.adapters.notion.demo dump      # rewrite the seed

``teammate_edit`` is the Notion step of demo/teammate_edit.py: a person fixing one price in the
table by hand, straight in Notion. The rogue agent rewrote that row too, so the rewind keeps the
teammate's price and flags it.
"""

from __future__ import annotations

import asyncio
import json
import sys

from mulligan.adapters.notion.adapter import NotionAdapter, flat, record
from mulligan.adapters.notion.normalize import CELL_SEP, block_form, form_text, rich_text
from mulligan.settings import Settings

PRODUCT = "Widget Basic"
TEAMMATE_PRICE = "$15"


async def teammate_edit(
    adapter: NotionAdapter, product: str = PRODUCT, price: str = TEAMMATE_PRICE
) -> str:
    """Set ``product``'s price in the pricing table to ``price``, writing to Notion directly.
    Returns a line saying what changed."""
    page_id = adapter.default_page_id
    if not page_id:
        raise RuntimeError("NOTION_PRICING_PAGE_ID is not set")
    snap = await adapter.snapshot(page_id)
    for node in flat(snap["blocks"]):
        text = form_text(block_form(node)) or ""
        if node["type"] == "table_row" and text.split(CELL_SEP)[0].strip() == product:
            cells = [rich_text(product), rich_text(price)]
            await adapter.client.update_block(node["id"], table_row={"cells": cells})
            return f"notion: {product} price {text.split(CELL_SEP)[-1]} -> {price}"
    raise RuntimeError(f"no {product!r} row in the pricing table")


async def _main(cmd: str) -> None:
    settings = Settings.from_env()
    adapter = await NotionAdapter.open(settings)
    try:
        if cmd == "teammate":
            print(await teammate_edit(adapter))
        elif cmd == "reset":
            await adapter.reset()
            print(f"notion: reset, checksum {await adapter.checksum()}")
        elif cmd == "dump":
            assert adapter.default_page_id
            rec = record(await adapter.snapshot(adapter.default_page_id))
            adapter.seed_path.write_text(json.dumps(rec, indent=1, ensure_ascii=False) + "\n")
            print(f"notion: wrote {adapter.seed_path}")
        else:
            sys.exit(f"unknown command {cmd!r}: teammate, reset or dump")
    finally:
        await adapter.close()


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1] if len(sys.argv) > 1 else "teammate"))
