"""The reversibility policy from contracts/reversibility_policy.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from mulligan.interfaces import Policy
from mulligan.models import (
    HoldKind,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    System,
)


class PolicyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system: System
    label: ReversibilityLabel
    hold: HoldKind | None
    reason: str


class PolicyFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    tools: dict[str, PolicyEntry]


class PolicyError(RuntimeError):
    pass


class YamlPolicy(Policy):
    def __init__(self, entries: dict[str, PolicyEntry]) -> None:
        self._entries = dict(entries)

    @classmethod
    def load(cls, path: Path) -> YamlPolicy:
        data = PolicyFile.model_validate(yaml.safe_load(path.read_text()))
        if data.version != 1:
            raise PolicyError(f"unsupported policy version {data.version}")
        return cls(data.tools)

    def entry(self, tool: str) -> PolicyEntry | None:
        return self._entries.get(tool)

    def lookup(self, tool: str) -> Reversibility | None:
        entry = self._entries.get(tool)
        if entry is None:
            return None
        return Reversibility(
            label=entry.label, source=ReversibilitySource.policy, reason=entry.reason
        )

    def hold_kind(self, tool: str) -> HoldKind | None:
        entry = self._entries.get(tool)
        return entry.hold if entry else None

    def tools(self) -> frozenset[str]:
        return frozenset(self._entries)

    def check_covers(self, registered: set[str] | frozenset[str]) -> None:
        """Refuse to start if a registered built-in tool has no entry."""
        missing = sorted(set(registered) - self.tools())
        if missing:
            raise PolicyError(f"no reversibility policy entry for: {', '.join(missing)}")
