"""Messages between the Guardian and the worker."""

from __future__ import annotations

from typing import Any

from uagents import Model


class TaskRequest(Model):
    request_id: str
    task: str


class TaskResult(Model):
    request_id: str
    task: str
    steps: list[dict[str, Any]]
    action_ids: list[str]
    error: str = ""
