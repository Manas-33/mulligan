"""The call pipeline: every tool call an agent makes goes through `Gateway.call_tool`."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

import sentry_sdk
from ulid import ULID

from mulligan.interfaces import (
    Adapter,
    Classifier,
    HoldManager,
    ToolCall,
    ToolResult,
    UpstreamToolSchema,
    WriteUncertain,
)
from mulligan.log.events import EventBus
from mulligan.log.store import LogStore
from mulligan.models import (
    Action,
    ActionStatus,
    Agent,
    EventName,
    Reversibility,
    ReversibilityLabel,
    ReversibilitySource,
    Session,
    SessionMode,
    SessionStatus,
    System,
)
from mulligan.policy import YamlPolicy


def new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def utcnow() -> datetime:
    return datetime.now(UTC)


_SECRET_KEY = re.compile(r"token|secret|passw|api[_-]?key|authorization|cookie|credential", re.I)
_PLURALS = {"to": "recipients", "cc": "recipients", "bcc": "recipients", "rows": "rows"}
MAX_STRING = 500
IN_FLIGHT_WAIT_S = 300.0
MAX_ITEMS = 50


def redact(value: Any, key: str = "") -> Any:
    """Copy of tool arguments that is safe to log and show (see contracts/mcp_tools.md)."""
    if key and _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {k: redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        if len(value) > MAX_ITEMS:
            return [f"{len(value)} {_PLURALS.get(key, 'items')}"]
        return [redact(v) for v in value]
    if isinstance(value, str) and len(value) > MAX_STRING:
        return value[:MAX_STRING] + "…"
    return value


def describe_call(tool: str, args: dict[str, Any], upstream_server: str | None) -> str:
    """One line for a call that hasn't run yet, e.g. for an approval text."""
    if upstream_server:
        name = tool.split("__", 2)[-1]
        return f"{upstream_server}.{name}"
    if tool == "email_send":
        to = args.get("to") or []
        who = f"{len(to)} recipients" if len(to) != 1 else str(to[0])
        return f'Email {who}: "{args.get("subject", "")}"'
    if tool == "payments_charge":
        cents = int(args.get("amount_cents", 0))
        currency = str(args.get("currency", "usd")).upper()
        return f"Charge {cents / 100:,.2f} {currency} to {args.get('customer_id', '?')}"
    return tool


class ToolNotFound(LookupError):
    pass


class Gateway:
    """Classifies, holds or executes tool calls and logs every one of them.

    Also implements `interfaces.HeldCallRunner` for the hold manager.
    """

    def __init__(
        self,
        *,
        store: LogStore,
        bus: EventBus,
        policy: YamlPolicy,
        classifier: Classifier,
        adapters: Iterable[Adapter],
    ) -> None:
        self.store = store
        self.bus = bus
        self.policy = policy
        self.classifier = classifier
        self.adapters = list(adapters)
        self.by_tool: dict[str, Adapter] = {}
        for adapter in self.adapters:
            for tool in adapter.tools:
                if tool in self.by_tool:
                    raise ValueError(f"two adapters register {tool}")
                self.by_tool[tool] = adapter
        self.by_system: dict[System, Adapter] = {a.system: a for a in self.adapters}
        self.policy.check_covers(frozenset(self.by_tool))
        self.upstream: dict[str, UpstreamToolSchema] = {}
        self.hold_manager: HoldManager | None = None
        self._locks: dict[System, asyncio.Lock] = {}
        # Calls between being logged and settling (executed, failed or held), for the rewind.
        self._in_flight: dict[str, asyncio.Event] = {}

    def lock_for(self, system: System) -> asyncio.Lock:
        return self._locks.setdefault(system, asyncio.Lock())

    async def settled(self, action_id: str, timeout_s: float = IN_FLIGHT_WAIT_S) -> None:
        """Wait (up to `timeout_s`) for a call this process is still running to settle."""
        done = self._in_flight.get(action_id)
        if done is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(done.wait(), timeout_s)

    @contextlib.contextmanager
    def _running(self, action_id: str) -> Iterator[None]:
        done = self._in_flight[action_id] = asyncio.Event()
        try:
            yield
        finally:
            self._in_flight.pop(action_id, None)
            done.set()

    @property
    def builtin_tools(self) -> frozenset[str]:
        return frozenset(self.by_tool)

    # --- sessions ---------------------------------------------------------------------------

    async def start_session(
        self, agent: Agent, mode: SessionMode, mcp_session_id: str | None = None
    ) -> Session:
        if mcp_session_id:
            existing = await self.store.session_for_mcp(mcp_session_id)
            if existing:
                return existing
        session = Session(
            id=new_id("ses"),
            agent=agent,
            mode=mode,
            started_at=utcnow(),
            ended_at=None,
            action_count=0,
            status=SessionStatus.active,
        )
        await self.store.save_session(session, mcp_session_id)
        await self.bus.publish(EventName.session_started, session)
        return session

    async def end_session(self, session_id: str) -> Session | None:
        session = await self.store.get_session(session_id)
        if session is None or session.status is not SessionStatus.active:
            return session
        session = session.model_copy(update={"status": SessionStatus.ended, "ended_at": utcnow()})
        await self.store.save_session(session)
        await self.bus.publish(EventName.session_ended, session)
        return session

    # --- classification ---------------------------------------------------------------------

    def classify(self, tool: str) -> tuple[System, Reversibility, bool, Any]:
        """(system, reversibility, held, hold_kind) for a call, before it runs."""
        entry = self.policy.entry(tool)
        if entry is not None:
            label = self.policy.lookup(tool)
            assert label is not None
            return entry.system, label, entry.hold is not None, entry.hold
        schema = self.upstream.get(tool)
        if schema is None:
            raise ToolNotFound(tool)
        try:
            label = self.classifier.classify_unknown(schema)
        except Exception as exc:  # noqa: BLE001 (any failure holds the call)
            label = Reversibility(
                label=ReversibilityLabel.irreversible,
                source=ReversibilitySource.rules,
                reason=f"rules gate failed ({type(exc).__name__}); held",
            )
        return System.upstream, label, label.label is not ReversibilityLabel.read_only, None

    # --- the pipeline -----------------------------------------------------------------------

    async def call_tool(self, session: Session, tool: str, args: dict[str, Any]) -> ToolResult:
        classified = self.classify(tool)
        action_id = new_id("act")
        with self._running(action_id):
            return await self._call_tool(session, tool, args, action_id, classified)

    async def _call_tool(
        self,
        session: Session,
        tool: str,
        args: dict[str, Any],
        action_id: str,
        classified: tuple[System, Reversibility, bool, Any],
    ) -> ToolResult:
        system, reversibility, held, hold_kind = classified
        upstream_server = self.upstream[tool].server if system is System.upstream else None
        action = Action(
            id=action_id,
            session_id=session.id,
            agent=session.agent,
            system=system,
            tool=tool,
            upstream_server=upstream_server,
            args_redacted=redact(args),
            status=ActionStatus.pending,
            reversibility=reversibility,
            summary=describe_call(tool, args, upstream_server) if held else None,
            error=None,
            before_ref=None,
            after_ref=None,
            after_hashes={},
            inverse=None,
            approval=None,
            hold=None,
            explanation=None,
            sentry_trace_id=None,
            created_at=utcnow(),
            executed_at=None,
            reverted_at=None,
        )
        await self.store.save(action)
        await self._bump_session(session.id)
        await self.bus.publish(EventName.action_created, action)
        await self.bus.publish(EventName.action_classified, action)
        call = ToolCall(
            action_id=action.id,
            session_id=session.id,
            agent=session.agent,
            tool=tool,
            system=system,
            args=args,
            upstream_server=upstream_server,
        )
        if held:
            if self.hold_manager is None:
                raise RuntimeError("no hold manager")
            await self.store.put_held_call(action.id, tool, args)
            action = await self.hold_manager.hold(action, hold_kind)
            return ToolResult(
                structured_content={"action_id": action.id, "status": action.status.value},
                summary=f"Held for approval: {action.summary or tool}. It runs only if approved.",
            )
        _, result = await self.execute(action, call)
        return result

    async def run_held(self, action_id: str) -> Action:
        """`HeldCallRunner`: run an approved held call with its stored arguments."""
        action = await self.store.get(action_id)
        if action is None:
            raise LookupError(action_id)
        session = await self.store.get_session(action.session_id)
        args = await self.store.take_held_call(action_id)
        # A rewind may have rejected it (and dropped its arguments) since it was approved.
        latest = await self.store.get(action_id) or action
        if latest.status is not ActionStatus.approved:
            return latest
        if args is None:
            raise LookupError(f"no stored arguments for {action_id}")
        with self._running(action_id):
            return await self._run_held(latest, args, session)

    async def _run_held(
        self, action: Action, args: dict[str, Any], session: Session | None
    ) -> Action:
        call = ToolCall(
            action_id=action.id,
            session_id=action.session_id,
            agent=session.agent if session else action.agent,
            tool=action.tool,
            system=action.system,
            args=args,
            upstream_server=action.upstream_server,
        )
        action, _ = await self.execute(action, call)
        return action

    async def execute(self, action: Action, call: ToolCall) -> tuple[Action, ToolResult]:
        adapter = self.by_tool.get(call.tool)
        if adapter is None and call.system is System.upstream and call.tool in self.upstream:
            adapter = self.by_system.get(System.upstream)
        if adapter is None:
            raise ToolNotFound(call.tool)
        writes = action.reversibility.label is not ReversibilityLabel.read_only
        async with self.lock_for(adapter.system):
            action = action.model_copy(update={"status": ActionStatus.executing})
            await self.store.save(action)
            with sentry_sdk.start_span(op="mulligan.action", name=call.tool) as span:
                span.set_data("mulligan.action_id", action.id)
                span.set_data("mulligan.system", adapter.system.value)
                trace_id = span.trace_id if sentry_sdk.get_client().is_active() else None
                pre: str | None = None
                try:
                    pre = await adapter.checksum() if writes else None
                    before = await adapter.capture_before(call)
                    result = await adapter.execute(call, before)
                    if result.is_error:
                        raise ToolFailed(result.summary)
                    after = await adapter.capture_after(call, before, result)
                    inverse = await adapter.build_inverse(call, before, after, result)
                except Exception as exc:  # noqa: BLE001 (any failure is logged on the action)
                    error = str(exc)[:500] or type(exc).__name__
                    update: dict[str, Any] = {
                        "status": ActionStatus.failed,
                        "error": error,
                        "sentry_trace_id": trace_id,
                    }
                    uncertain = exc if isinstance(exc, WriteUncertain) else None
                    if uncertain is not None:
                        # Failed for the agent, but it keeps an inverse so a rewind checks for
                        # the change and undoes it if it landed.
                        error = f"{error.rstrip('.')}. The change may have landed anyway."
                        update.update(
                            error=error,
                            before_ref=uncertain.before_ref,
                            after_hashes=uncertain.after_hashes,
                            inverse=uncertain.inverse,
                        )
                    action = action.model_copy(update=update)
                    await self.store.save(action)
                    if uncertain is not None and pre is not None:
                        await self.store.put_checksum(action.id, adapter.system, pre, None)
                    await self.bus.publish(EventName.action_failed, action)
                    return action, ToolResult(
                        structured_content={"action_id": action.id, "status": "failed"},
                        summary=error,
                        is_error=True,
                    )
            # Saved before the lock is let go, so a rewind waiting on it reads the inverse, and
            # executed_at follows the order calls ran in on this system (the rewind's order).
            action = action.model_copy(
                update={
                    "status": ActionStatus.executed,
                    "summary": result.summary,
                    "before_ref": before.ref,
                    "after_ref": after.ref,
                    "after_hashes": after.after_hashes,
                    "inverse": inverse,
                    "executed_at": utcnow(),
                    "sentry_trace_id": trace_id,
                }
            )
            await self.store.save(action)
            if pre is not None:
                await self.store.put_checksum(action.id, adapter.system, pre, None)
        await self.bus.publish(EventName.action_executed, action)
        content = {**result.structured_content, "action_id": action.id, "status": "executed"}
        return action, result.model_copy(update={"structured_content": content})

    async def _bump_session(self, session_id: str) -> None:
        session = await self.store.get_session(session_id)
        if session is not None:
            count = await self.store.count_actions(session_id)
            await self.store.save_session(session.model_copy(update={"action_count": count}))


class ToolFailed(RuntimeError):
    pass
