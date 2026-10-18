"""Reset the demo: reseed prod-db (and every other adapter that resets), clear the log, print
checksums. Calls POST /api/demo/reset, which needs MULLIGAN_DEMO=1 on the gateway.

  uv --directory gateway run python ../demo/scripts/reset.py
"""

from __future__ import annotations

import os
import sys

import httpx

from mulligan.settings import REPO_ROOT, load_dotenv

for key, value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(key, value)

url = (
    os.environ.get("MULLIGAN_API_URL")
    or f"http://127.0.0.1:{os.environ.get('MULLIGAN_API_PORT', '8000')}"
)
token = os.environ.get("MULLIGAN_DASHBOARD_TOKEN", "")
r = httpx.post(f"{url}/api/demo/reset", headers={"Authorization": f"Bearer {token}"}, timeout=120)
if r.status_code != 200:
    sys.exit(f"reset failed: {r.status_code} {r.text}")
for system, check in r.json()["checksums"].items():
    print(f"{system:<9} {check['hash']}")
