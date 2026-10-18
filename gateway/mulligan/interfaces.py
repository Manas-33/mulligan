"""The seams between the gateway core (Stream A) and the parts that plug into it.

Stream A implements the core, the action log, the event bus, the Postgres/GitHub/Notion adapters
and the YAML policy. Stream B implements, inside ``holds/`` and ``classify/``:

* ``holds/__init__.py``: ``create_hold_manager(deps: HoldDeps) -> HoldManager`` and
  ``create_adapters() -> list[Adapter]``: the email (system ``email``) and payments (system
  ``payments``) adapters. They execute approved calls and undo them like any other adapter.
* ``classify/__init__.py``: ``create_classifier() -> Classifier`` (the rules gate).

Until those exist the core uses its own stubs: a classifier that holds everything and a hold
manager that rejects every held call.

How a call flows:

1. The MCP server builds a ``ToolCall`` and logs an ``Action`` (``pending``).
2. ``Policy.lookup(tool)`` labels a built-in tool. For an upstream tool it returns ``None`` and
   ``Classifier.classify_unknown`` labels it instead. Only ``read_only`` from the classifier runs
   straight through; any other label, or an exception, holds the call (fail closed).
3. A call that needs no hold runs through its ``Adapter``: ``capture_before`` -> ``execute`` ->
   ``capture_after`` -> ``build_inverse``.
4. A held call goes to ``HoldManager.hold``. The core keeps its unredacted arguments. When someone
   approves it, the hold manager calls ``HeldCallRunner.run_held``, which runs step 3.
5. A rewind walks the actions newest first. An action that hasn't run yet (``held`` or
   ``approved``) goes to ``HoldManager.cancel``. One that ran goes to its system's adapter,
   ``Adapter.plan_revert`` (dry run) or ``Adapter.revert``, including email and payments: the
   payments adapter cancels an authorization that wasn't captured and refunds one that was
   (``compensated``), and the email adapter returns ``skipped`` for a sent email, or
   ``compensated`` if it sends a correction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mulligan.models import (
    Action,
    Agent,
    ApprovalDecisionRequest,
    EventName,
    HoldKind,
    Inverse,
    Reversibility,
    RewindStep,
    System,
)


class ToolCall(BaseModel):
    """One tool call on its way through the gateway.

    ``args`` are the real, unredacted arguments, already validated against the tool's input
    schema. Never log or return them; the action log only gets ``Action.args_redacted``.
    """

    model_config = ConfigDict(frozen=True)

    action_id: str
    session_id: str
    agent: Agent
    tool: str
    system: System
    args: dict[str, Any]
    upstream_server: str | None = None


class ToolResult(BaseModel):
    """What a tool call returns to the agent."""

    structured_content: dict[str, Any]
    summary: str
    """One line for ``Action.summary`` and the agent's text block."""
    is_error: bool = False


class Capture(BaseModel):
    """State an adapter captured around one call.

    ``ref`` is stored as ``Action.before_ref`` / ``Action.after_ref``. ``after_hashes`` (only on
    the after capture) becomes ``Action.after_hashes``. ``state`` is private to the adapter, kept in
    memory between the calls for one action and never persisted.
    """

    ref: str | None = None
    after_hashes: dict[str, str] = Field(default_factory=dict)
    state: dict[str, Any] = Field(default_factory=dict)


class Adapter(ABC):
    """Executes and undoes the calls for one system.

    The core calls ``capture_before``, ``execute``, ``capture_after`` and ``build_inverse`` in that
    order for one call, and never runs two calls on the same adapter at the same time.
    """

    system: ClassVar[System]
    tools: ClassVar[frozenset[str]]
    """The built-in tool names this adapter executes."""

    @abstractmethod
    async def capture_before(self, call: ToolCall) -> Capture:
        """Capture what the call is about to change."""

    @abstractmethod
    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        """Run the call against the real system. Raise to fail it."""

    @abstractmethod
    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        """Capture the after-state and hash every field the call changed."""

    @abstractmethod
    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        """The recipe ``revert`` will follow, or ``None`` if there is nothing to undo."""

    @abstractmethod
    async def plan_revert(self, action: Action) -> RewindStep:
        """Dry run of ``revert``: no side effects.

        Returns a step with ``result`` ``pending`` and ``changes`` filled in, marking each field
        ``restore`` or ``keep_human_edit`` exactly as ``revert`` would.
        """

    @abstractmethod
    async def revert(self, action: Action) -> RewindStep:
        """Undo one action.

        Re-read every field in ``action.after_hashes``. Restore a field only if its current hash
        still matches; otherwise a human changed it, so leave it, list it in ``kept_fields`` and
        return ``kept_human_edit``. Nothing is merged. Return ``skipped`` when there is nothing safe
        to do, and ``failed`` (with ``detail``) instead of raising.

        An effect that can't be undone but can be offset returns ``compensated``: the payments
        adapter refunds a captured charge (inverse ``payments.refund``; an uncaptured one is
        cancelled with ``payments.cancel_authorization`` and is ``restored``), and the email
        adapter may send a correction for a sent email (``email.send_correction``).
        """

    @abstractmethod
    async def checksum(self) -> str | None:
        """Hash of the system's canonical, sorted state, or ``None`` if it has none."""


class Policy(ABC):
    """The reversibility policy for built-in tools (``contracts/reversibility_policy.yaml``)."""

    @abstractmethod
    def lookup(self, tool: str) -> Reversibility | None:
        """The tool's label with ``source`` ``policy``, or ``None`` if it isn't a built-in tool."""

    @abstractmethod
    def hold_kind(self, tool: str) -> HoldKind | None:
        """``email_outbox`` or ``payment_auth`` if the tool always waits for approval."""

    @abstractmethod
    def tools(self) -> frozenset[str]:
        """Every tool with an entry. The gateway won't start if a registered tool is missing."""


class ToolAnnotations(BaseModel):
    """MCP tool annotations as the upstream server reported them (all optional hints)."""

    model_config = ConfigDict(extra="allow")

    title: str | None = None
    readOnlyHint: bool | None = None  # noqa: N815 (MCP's field names)
    destructiveHint: bool | None = None  # noqa: N815
    idempotentHint: bool | None = None  # noqa: N815
    openWorldHint: bool | None = None  # noqa: N815


class UpstreamToolSchema(BaseModel):
    """A tool from an MCP server behind Mulligan, as the classifier sees it."""

    model_config = ConfigDict(frozen=True)

    server: str
    """The upstream server's name from ``UPSTREAM_MCP_SERVERS``, e.g. ``slack``."""
    name: str
    """The upstream server's ORIGINAL tool name, e.g. ``post_message``. Never the exposed
    ``upstream__slack__post_message``: that prefix would hide the read verb from the rules."""
    exposed_name: str
    """What agents see: ``upstream__<server>__<name>``."""
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    annotations: ToolAnnotations | None = None


class Classifier(ABC):
    """Labels upstream tools, which have no policy entry. Deterministic rules, not a model."""

    @abstractmethod
    def classify_unknown(self, tool_schema: UpstreamToolSchema) -> Reversibility:
        """Return a label with ``source`` ``rules`` and ``reason`` naming the rule that fired.

        Called only when ``Policy.lookup`` returned ``None``. The gateway lets the call through
        only for ``read_only``; any other label, or an exception, holds it.
        """


class ActionStore(Protocol):
    """The action log, as the hold manager sees it."""

    async def get(self, action_id: str) -> Action | None: ...

    async def save(self, action: Action) -> None:
        """Insert or replace the action."""
        ...

    async def list_by_status(self, *statuses: str) -> list[Action]: ...


class EventBus(Protocol):
    async def publish(self, event: EventName, data: BaseModel) -> int:
        """Assign ``seq`` and ``ts``, store the event and fan it out to SSE. Returns ``seq``."""
        ...


class HeldCallRunner(Protocol):
    async def run_held(self, action_id: str) -> Action:
        """Run an approved held call with its stored, unredacted arguments.

        Goes through the tool's adapter (or the upstream server), updates the log and publishes
        ``action.executed`` or ``action.failed``. Returns the updated action.
        """
        ...


@dataclass(frozen=True)
class HoldDeps:
    """What the core hands to ``create_hold_manager``."""

    store: ActionStore
    bus: EventBus
    runner: HeldCallRunner


class HoldError(Exception):
    """Base for decision errors. The REST API maps them to status codes."""


class HoldNotFound(HoldError):
    """No such action (404)."""


class NotHeld(HoldError):
    """The action isn't waiting for a decision (409)."""


class WrongCode(HoldError):
    """The approval code doesn't match (422)."""


class HoldManager(ABC):
    """Approval state machine for held calls: held -> approved | rejected | expired."""

    @abstractmethod
    async def hold(self, action: Action, hold_kind: HoldKind | None) -> Action:
        """Put a call on hold.

        Assign an approval code that is unique among held actions, set ``approval`` (and ``hold``
        when ``hold_kind`` is set; it is ``None`` for upstream calls), save the action with status
        ``held``, publish ``action.held`` and text the approver. Return the saved action.

        If the Photon service answers 409 ``approver_not_reachable`` (the approver hasn't texted
        the shared line yet), don't retry: the action stays ``held`` and can still be decided from
        the dashboard or ASI:One.
        """

    @abstractmethod
    async def on_decision(self, action_id: str, decision: ApprovalDecisionRequest) -> Action:
        """Record an approve or reject from iMessage, the dashboard or ASI:One.

        Raise ``HoldNotFound``, ``NotHeld`` or ``WrongCode``. On approve, publish
        ``action.approved``, then call ``HeldCallRunner.run_held``. On reject, publish
        ``action.rejected``. Return the action's latest state.
        """

    @abstractmethod
    async def release_due(self) -> list[Action]:
        """Called by the core every few seconds.

        Expire holds past ``approval.expires_at`` (``hold.expired``) and capture payments past
        ``hold.release_at`` (``hold.released``). Return the actions it changed.

        A payment's ``hold.release_at`` must be earlier than ``hold.authorization_expires_at``
        (Stripe's ``capture_before``), or the authorization lapses before the capture: set it that
        way when authorizing, and capture early rather than miss the window.
        """

    @abstractmethod
    async def cancel(self, action: Action) -> RewindStep:
        """Rewind an action that hasn't run yet (held or approved): reject it and drop the hold.

        Only for actions that haven't run. Undoing an email that was sent or a payment that was
        authorized or captured is the email or payments adapter's ``revert``.
        """
