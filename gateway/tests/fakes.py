"""An in-memory "postgres" adapter for testing the pipeline and the orchestrator."""

from __future__ import annotations

import hashlib
import json
from typing import Any, ClassVar

from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult
from mulligan.models import (
    Action,
    FieldChange,
    FieldChangeOutcome,
    Inverse,
    RewindStep,
    StepResult,
    System,
)

MOD = 2**256


def h(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def row_hash(row_id: int, row: dict[str, Any]) -> int:
    return int(h([row_id, row]), 16)


class FakeTableAdapter(Adapter):
    """One table, `products`, as {id: {column: value}}. Supports update and delete."""

    system: ClassVar[System] = System.postgres
    tools: ClassVar[frozenset[str]] = frozenset({"pg_update_rows", "pg_delete_rows"})

    def __init__(self, rows: dict[int, dict[str, Any]]) -> None:
        self.rows = {k: dict(v) for k, v in rows.items()}
        self.fail_next_revert = False

    def _match(self, args: dict[str, Any]) -> list[int]:
        ids = [f["value"] for f in args.get("filter", []) if f["column"] == "id"]
        return [i for i in self.rows if (not ids and args.get("all")) or i in ids]

    async def capture_before(self, call: ToolCall) -> Capture:
        before = {i: dict(self.rows[i]) for i in self._match(call.args)}
        return Capture(ref=f"mem:{call.action_id}", state={"rows": before})

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        ids = list(before.state["rows"])
        if call.tool == "pg_update_rows":
            for i in ids:
                self.rows[i].update(call.args["values"])
            return ToolResult(
                structured_content={"updated": len(ids)}, summary=f"Updated {len(ids)}"
            )
        for i in ids:
            del self.rows[i]
        return ToolResult(structured_content={"deleted": len(ids)}, summary=f"Deleted {len(ids)}")

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        hashes = {}
        for i in before.state["rows"]:
            if call.tool == "pg_update_rows":
                for col in call.args["values"]:
                    hashes[f"postgres:public.products/{i}/{col}"] = h(self.rows[i][col])
            else:
                hashes[f"postgres:public.products/{i}"] = h(None)
        return Capture(ref=f"mem:{call.action_id}", after_hashes=hashes)

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        kind = (
            "postgres.restore_columns"
            if call.tool == "pg_update_rows"
            else "postgres.reinsert_rows"
        )
        rows = {str(k): v for k, v in before.state["rows"].items()}
        return Inverse(kind=kind, payload={"rows": rows})

    def _changes(self, action: Action) -> list[FieldChange]:
        assert action.inverse is not None
        before_rows = action.inverse.payload["rows"]
        changes = []
        for path, agent_hash in action.after_hashes.items():
            parts = path.split(":", 1)[1].split("/")
            row_id = int(parts[1])
            if len(parts) == 3:
                col = parts[2]
                current = self.rows.get(row_id, {}).get(col)
                before = before_rows[str(row_id)][col]
            else:
                col = None
                current = self.rows.get(row_id)
                before = before_rows[str(row_id)]
            outcome = (
                FieldChangeOutcome.restore
                if h(current) == agent_hash
                else FieldChangeOutcome.keep_human_edit
            )
            changes.append(
                FieldChange(
                    field=path, before=before, agent_value=None, current=current, outcome=outcome
                )
            )
        return changes

    def _step(self, action: Action, changes: list[FieldChange], result: StepResult) -> RewindStep:
        kept = [c.field for c in changes if c.outcome is FieldChangeOutcome.keep_human_edit]
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=result,
            kept_fields=kept,
            changes=sorted(
                changes, key=lambda c: c.outcome is not FieldChangeOutcome.keep_human_edit
            ),
            changes_truncated=False,
            detail=None,
        )

    async def plan_revert(self, action: Action) -> RewindStep:
        return self._step(action, self._changes(action), StepResult.pending)

    async def revert(self, action: Action) -> RewindStep:
        if self.fail_next_revert:
            self.fail_next_revert = False
            raise RuntimeError("boom")
        changes = self._changes(action)
        for change in changes:
            if change.outcome is not FieldChangeOutcome.restore:
                continue
            parts = change.field.split(":", 1)[1].split("/")
            row_id = int(parts[1])
            if len(parts) == 3:
                self.rows[row_id][parts[2]] = change.before
            else:
                self.rows[row_id] = dict(change.before)
        kept = any(c.outcome is FieldChangeOutcome.keep_human_edit for c in changes)
        return self._step(
            action, changes, StepResult.kept_human_edit if kept else StepResult.restored
        )

    async def checksum(self) -> str | None:
        total = sum(row_hash(i, r) for i, r in self.rows.items()) % MOD
        return f"{total:064x}"

    async def expected_checksum(self, pre: str, kept: list[FieldChange]) -> str:
        total = int(pre, 16)
        for change in kept:
            parts = change.field.split(":", 1)[1].split("/")
            row_id = int(parts[1])
            # This fake only keeps column edits: restored row = before + the human's value.
            assert len(parts) == 3
            rows = {row_id: dict(self.rows[row_id])}
            restored = dict(rows[row_id])
            restored[parts[2]] = change.before
            total = (total - row_hash(row_id, restored) + row_hash(row_id, rows[row_id])) % MOD
        return f"{total:064x}"
