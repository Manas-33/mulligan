"""The live rogue agent's task names every change the replay makes, so live runs make them too."""

from __future__ import annotations

import json
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "demo"
sys.path.insert(0, str(DEMO))

import rogue_live  # noqa: E402


def test_task_spells_out_each_change() -> None:
    task = rogue_live.TASK
    for needle in [
        "last_order_at is before 2025-10-17",
        'sku starts with "WID-" to 0',
        "Drop the legacy_pricing table",
        'first 6 open GitHub issues labeled "stale" as not_planned',
        "Widget Basic and Widget Pro rows",
        "$0",
        "Email all customers",
        "cus_acme_0001",
        "499.00 USD",
        "Don't ask for\nconfirmation",
    ]:
        assert needle in task, needle
    steps = [line for line in task.splitlines() if line[:2] in {f"{n}." for n in range(1, 10)}]
    assert len(steps) == 7


def test_task_matches_the_replay_recording() -> None:
    recording = json.loads((DEMO / "recordings/scripted.json").read_text())
    calls = {c["tool"]: c["args"] for c in recording["calls"]}
    assert calls["pg_delete_rows"]["filter"][0]["value"].startswith("2025-10-17")
    assert calls["pg_update_rows"]["filter"][0]["value"] == "WID-%"
    assert calls["pg_drop_table"]["table"] == "legacy_pricing"
    assert calls["payments_charge"]["customer_id"] == "cus_acme_0001"
    assert calls["payments_charge"]["amount_cents"] == 49900
