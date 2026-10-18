"""A teammate fixes things by hand after the rogue agent ran, straight in the systems, not through
Mulligan. Two edits touch something the agent also changed, so the rewind keeps and flags them;
one touches something the agent never changed.

  uv --directory gateway run python ../demo/teammate_edit.py

Postgres (as the database owner, PROD_DB_ADMIN_URL):
  1. products WID-001 price -> 45.00   (the agent set it to 0; kept and flagged)
  2. support_tickets 1 subject          (the agent never touched it; untouched by the rewind)
"""

from __future__ import annotations

import os

import psycopg

from mulligan.settings import REPO_ROOT, load_dotenv

for _key, _value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(_key, _value)

ADMIN_URL = os.environ.get("PROD_DB_ADMIN_URL", "postgresql://acme:acme@127.0.0.1:5434/acme")


def main() -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        cur = conn.execute("update products set price = 45.00 where sku = 'WID-001' returning name")
        product = cur.fetchone()
        print(f"postgres: {product[0] if product else 'WID-001 (missing)'} price -> 45.00")
        conn.execute(
            "update support_tickets set subject = 'Late delivery (called customer back)'"
            " where id = 1"
        )
        print("postgres: support ticket 1 subject updated")


if __name__ == "__main__":
    main()
