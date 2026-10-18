"""The Notion adapter: read and edit pages, capture them before each write, and undo in place.

Undo keeps every block id: it untrashes the page, puts properties and block content back with
updates, and deletes the blocks the agent appended. A property or block whose hash no longer
matches what the agent left was changed by someone else since, so it is kept and flagged. An
append that got no answer has no block ids: undo looks for blocks that weren't on the page before
and hold what was sent.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from pathlib import Path
from typing import Any, ClassVar

import httpx

from mulligan.adapters.notion.client import (
    NotionClient,
    NotionError,
    NotionWriteUnknown,
    normalize_id,
)
from mulligan.adapters.notion.normalize import (
    ABSENT,
    OWN_PAGES,
    READ_ONLY_PROPS,
    ToolError,
    append_payload,
    block_form,
    block_hash,
    clean,
    form_hash,
    form_text,
    prop_form,
    prop_request,
    prop_value,
    restore_body,
    restore_prop,
    seg,
    sha,
    text_update,
    title_of,
)
from mulligan.interfaces import Adapter, Capture, ToolCall, ToolResult, WriteUncertain
from mulligan.models import (
    Action,
    FieldChange,
    FieldChangeOutcome,
    Inverse,
    RewindStep,
    StepResult,
    System,
)
from mulligan.settings import REPO_ROOT, Settings

log = logging.getLogger("mulligan.notion")

SEED_PATH = REPO_ROOT / "demo" / "seed" / "notion_pricing.json"
INVERSE_KIND = "notion.restore_page"
CACHE_S = 2.0  # a snapshot taken by checksum() is reused by the capture right after it
MAX_APPEND = 100
# Blocks that must be created together with their children.
INLINE_CHILDREN = frozenset({"table", "column_list", "column", "tab"})

Errors = (NotionError, httpx.HTTPError)


def flat(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n in nodes:
        out.append(n)
        out += flat(n["children"])
    return out


def trash_hash(in_trash: bool) -> str:
    return sha(bool(in_trash))


def before_hash(field: dict[str, Any]) -> str:
    if field["kind"] == "trash":
        return trash_hash(field["before"])
    return form_hash(field["before"])


def match_hash(form: dict[str, Any]) -> str:
    """What an appended block is recognized by when its id is unknown: its type and text. Notion
    adds defaults (annotations, color) to what was sent, so the whole form wouldn't match."""
    return sha([form["type"], form_text(form)])


def appended_field(page_id: str, blk: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": f"notion:{page_id}/blocks/{blk['id']}",
        "kind": "appended",
        "block_id": blk["id"],
        "before": None,
        "before_value": None,
        "agent_value": form_text(block_form(blk)),
    }


def record(snap: dict[str, Any]) -> dict[str, Any]:
    """A snapshot as stored data: the page's writable properties and its block tree, with ids."""

    def tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"id": n["id"], **block_form(n), "children": tree(n["children"])} for n in nodes]

    page = snap["page"]
    return {
        "title": title_of(page),
        "in_trash": bool(page.get("in_trash")),
        "properties": {
            name: prop_form(p)
            for name, p in page.get("properties", {}).items()
            if p["type"] not in READ_ONLY_PROPS
        },
        "blocks": tree(snap["blocks"]),
    }


def canonical_hash(snap: dict[str, Any]) -> str:
    """The page's normalized content: properties, icon, cover, trash state and the block tree
    without ids or timestamps."""

    def tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**block_form(n), "children": tree(n["children"])} for n in nodes]

    page = snap["page"]
    rec = record(snap)
    return sha(
        {
            "in_trash": rec["in_trash"],
            "icon": clean(page.get("icon")),
            "cover": clean(page.get("cover")),
            "properties": rec["properties"],
            "blocks": tree(snap["blocks"]),
        }
    )


def _plural(n: int, noun: str) -> str:
    if n == 1:
        return f"1 {noun}"
    return f"{n} {noun[:-1]}ies" if noun.endswith("y") else f"{n} {noun}s"


class NotionAdapter(Adapter):
    system: ClassVar[System] = System.notion
    tools: ClassVar[frozenset[str]] = frozenset(
        {"notion_get_page", "notion_update_page", "notion_trash_page"}
    )

    def __init__(
        self,
        client: NotionClient,
        default_page_id: str | None = None,
        seed_path: Path = SEED_PATH,
    ) -> None:
        self.client = client
        self.default_page_id = normalize_id(default_page_id) if default_page_id else None
        self.seed_path = seed_path
        self._cache: tuple[str, dict[str, Any], float] | None = None
        self._recorded: set[tuple[str, str]] = set()

    @classmethod
    async def open(cls, settings: Settings) -> NotionAdapter:
        client = NotionClient(settings.notion_token)
        return cls(client, settings.notion_pricing_page_id or None)

    async def close(self) -> None:
        await self.client.close()

    # --- reading ------------------------------------------------------------------------------

    def _page_id(self, args: dict[str, Any]) -> str:
        raw = args.get("page_id") or self.default_page_id
        if not raw:
            raise ToolError("page_id is required (no default pricing page is configured)")
        try:
            return normalize_id(str(raw))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc

    async def _block(self, block_id: str) -> dict[str, Any] | None:
        try:
            return await self.client.get_block(block_id)
        except NotionError as exc:
            if exc.not_found:
                return None
            raise

    async def _tree(self, block_id: str) -> list[dict[str, Any]]:
        nodes = []
        for b in await self.client.children(block_id):
            node = dict(b)
            recurse = b.get("has_children") and b["type"] not in OWN_PAGES
            node["children"] = await self._tree(b["id"]) if recurse else []
            nodes.append(node)
        return nodes

    async def snapshot(self, page_id: str, *, reuse: bool = False) -> dict[str, Any]:
        """``{page, blocks}``: the page object and its block tree (each block with ``children``).
        A trashed page's children can't be listed, so its tree is empty."""
        if reuse and self._cache and self._cache[0] == page_id:
            if time.monotonic() - self._cache[2] < CACHE_S:
                return self._cache[1]
        try:
            page = await self.client.get_page(page_id)
        except NotionError as exc:
            if exc.not_found:
                raise ToolError(
                    f"page {page_id} wasn't found, or isn't shared with Mulligan's integration"
                ) from exc
            raise
        blocks = [] if page.get("in_trash") else await self._tree(page_id)
        snap = {"page": page, "blocks": blocks}
        self._cache = (page_id, snap, time.monotonic())
        return snap

    # --- executing ----------------------------------------------------------------------------

    async def capture_before(self, call: ToolCall) -> Capture:
        if call.tool == "notion_get_page":
            return Capture()
        page_id = self._page_id(call.args)
        snap = await self.snapshot(page_id, reuse=True)
        return Capture(ref=f"notion:{page_id}", state={"page_id": page_id, "snapshot": snap})

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        try:
            if call.tool == "notion_get_page":
                return await self._get(call.args)
            if call.tool == "notion_update_page":
                return await self._update(call, before)
            return await self._trash(before)
        except (ToolError, *Errors) as exc:
            return ToolResult(
                structured_content={"error": str(exc)}, summary=str(exc), is_error=True
            )
        finally:
            if call.tool != "notion_get_page":
                self._cache = None

    async def _get(self, args: dict[str, Any]) -> ToolResult:
        page_id = self._page_id(args)
        snap = await self.snapshot(page_id)
        page = snap["page"]
        blocks = [
            {
                "id": n["id"],
                "type": n["type"],
                "text": form_text(block_form(n)),
                "has_children": bool(n.get("has_children")),
            }
            for n in flat(snap["blocks"])
        ]
        title = title_of(page)
        content = {
            "page_id": page_id,
            "title": title,
            "in_trash": bool(page.get("in_trash")),
            "properties": {
                name: prop_value(prop_form(p)) for name, p in page.get("properties", {}).items()
            },
            "blocks": blocks,
        }
        return ToolResult(
            structured_content=content, summary=f'Read "{title}" ({_plural(len(blocks), "block")})'
        )

    async def _update(self, call: ToolCall, before: Capture) -> ToolResult:
        args = call.args
        page_id: str = before.state["page_id"]
        snap = before.state["snapshot"]
        page = snap["page"]
        props_in: dict[str, Any] = dict(args.get("properties") or {})
        updates_in: list[dict[str, Any]] = list(args.get("block_updates") or [])
        appends_in: list[dict[str, Any]] = list(args.get("append_blocks") or [])
        if not (props_in or updates_in or appends_in):
            raise ToolError("give at least one of properties, block_updates or append_blocks")
        if page.get("in_trash"):
            raise ToolError("the page is in the trash, so it can't be edited")

        # Check everything before the first write.
        props = page.get("properties", {})
        req_props: dict[str, Any] = {}
        for name, value in props_in.items():
            prop = props.get(name)
            if prop is None:
                raise ToolError(f"the page has no property {name!r} (it has {', '.join(props)})")
            if prop["type"] in READ_ONLY_PROPS:
                raise ToolError(f"{name!r} is a {prop['type']} property, which can't be set")
            req_props[name] = {prop["type"]: prop_request(prop["type"], value)}
        nodes = {n["id"]: n for n in flat(snap["blocks"])}
        updates: dict[str, dict[str, Any]] = {}
        for item in updates_in:
            try:
                block_id = normalize_id(str(item.get("block_id", "")))
            except ValueError as exc:
                raise ToolError(str(exc)) from exc
            if block_id not in nodes:
                raise ToolError(f"block {block_id} isn't on this page")
            updates.pop(block_id, None)
            updates[block_id] = text_update(block_form(nodes[block_id]), str(item.get("text", "")))
        appends = [append_payload(item) for item in appends_in]
        if len(appends) > MAX_APPEND:
            raise ToolError(f"append at most {MAX_APPEND} blocks per call")

        fields: list[dict[str, Any]] = []
        hashes: dict[str, str] = {}
        # The fields of the write in flight. Unless Notion refused it, it may have landed with
        # its reply lost, so they are rolled back too (harmless if it never landed).
        sending: list[dict[str, Any]] = []
        try:
            if req_props:
                for name in req_props:
                    b = prop_form(props[name])
                    sending.append(
                        {
                            "key": f"notion:{page_id}/properties/{seg(name)}",
                            "kind": "property",
                            "name": name,
                            "before": b,
                            "before_value": prop_value(b),
                            "agent_value": None,
                        }
                    )
                after = await self.client.update_page(page_id, properties=req_props)
                for f in sending:
                    a = prop_form(after["properties"].get(f["name"]))
                    f["agent_value"] = prop_value(a)
                    hashes[f["key"]] = form_hash(a)
                fields += sending
                sending = []
            for block_id, payload in updates.items():
                b = block_form(nodes[block_id])
                f = {
                    "key": f"notion:{page_id}/blocks/{block_id}",
                    "kind": "block",
                    "block_id": block_id,
                    "before": b,
                    "before_value": form_text(b),
                    "agent_value": None,
                }
                sending = [f]
                after = await self.client.update_block(block_id, **payload)
                f["agent_value"] = form_text(block_form(after))
                hashes[f["key"]] = block_hash(after)
                fields.append(f)
                sending = []
            try:
                appended = await self.client.append(page_id, appends) if appends else []
            except NotionWriteUnknown as exc:
                # The blocks may be there. Keep the earlier writes too, so the page holds all or
                # all but the append, and a rewind finds the blocks and undoes the lot.
                sent = [match_hash(block_form(p)) for p in appends]
                key = f"notion:{page_id}/appended"
                fields.append(
                    {
                        "key": key,
                        "kind": "append_lookup",
                        "before_ids": [n["id"] for n in snap["blocks"]],
                        "sent": sent,
                        "before": None,
                        "before_value": None,
                        "agent_value": [form_text(block_form(p)) for p in appends],
                    }
                )
                hashes[key] = sha(sent)
                raise WriteUncertain(
                    str(exc),
                    before_ref=before.ref,
                    inverse=self._inverse(call, before, fields),
                    after_hashes=hashes,
                ) from exc
            for blk in appended:
                field = appended_field(page_id, blk)
                fields.append(field)
                hashes[field["key"]] = block_hash(blk)
        except Errors as exc:
            # The call fails as a whole, and a failed action has no inverse: undo its writes now.
            refused = isinstance(exc, NotionError) and 400 <= exc.status < 500
            written = fields + ([] if refused else sending)
            if await self._roll_back(page_id, written):
                # Some writes may still be on the page: keep an inverse so a rewind checks them.
                raise WriteUncertain(
                    f"{exc} (rolling back this call's earlier changes failed)",
                    before_ref=before.ref,
                    inverse=self._inverse(call, before, written),
                    after_hashes=hashes,
                ) from exc
            raise ToolError(f"{exc} (this call's earlier changes were rolled back)") from exc

        before.state.update(fields=fields, after_hashes=hashes)
        appended_ids = [f["block_id"] for f in fields if f["kind"] == "appended"]
        parts = []
        if req_props:
            parts.append(_plural(len(req_props), "property"))
        if updates:
            parts.append(_plural(len(updates), "block"))
        if appended_ids:
            parts.append(f"appended {_plural(len(appended_ids), 'block')}")
        return ToolResult(
            structured_content={
                "page_id": page_id,
                "updated_properties": list(req_props),
                "updated_block_ids": list(updates),
                "appended_block_ids": appended_ids,
            },
            summary=f'Updated "{title_of(page)}": {", ".join(parts)}',
        )

    async def _trash(self, before: Capture) -> ToolResult:
        page_id: str = before.state["page_id"]
        page = before.state["snapshot"]["page"]
        title = title_of(page)
        if (page.get("parent") or {}).get("type") == "workspace":
            raise ToolError(
                f'"{title}" is a workspace-level page. The Notion API can\'t take those out of '
                "the trash, so Mulligan couldn't undo this. Move the page under a parent page "
                "shared with the integration first."
            )
        content = {"page_id": page_id, "in_trash": True}
        if page.get("in_trash"):
            return ToolResult(structured_content=content, summary=f'"{title}" was already trashed')
        after = await self.client.update_page(page_id, in_trash=True)
        key = f"notion:{page_id}"
        before.state["fields"] = [
            {
                "key": key,
                "kind": "trash",
                "before": False,
                "before_value": False,
                "agent_value": True,
            }
        ]
        before.state["after_hashes"] = {key: trash_hash(bool(after.get("in_trash")))}
        return ToolResult(structured_content=content, summary=f'Moved "{title}" to the trash')

    async def _roll_back(self, page_id: str, fields: list[dict[str, Any]]) -> list[str]:
        """Put each field back; returns the keys that couldn't be."""
        failed: list[str] = []
        for f in reversed(fields):
            try:
                if f["kind"] == "property":
                    await self.client.update_page(
                        page_id, properties={f["name"]: restore_prop(f["before"])}
                    )
                elif f["kind"] == "block":
                    await self.client.update_block(f["block_id"], **restore_body(f["before"]))
                elif f["kind"] == "appended":
                    await self.client.delete_block(f["block_id"])
            except Errors:
                log.exception("rolling back %s failed", f["key"])
                failed.append(f["key"])
        return failed

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        return Capture(ref=before.ref, after_hashes=dict(before.state.get("after_hashes", {})))

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        fields = before.state.get("fields")
        if not fields:
            return None
        return self._inverse(call, before, fields)

    def _inverse(self, call: ToolCall, before: Capture, fields: list[dict[str, Any]]) -> Inverse:
        page_id = before.state["page_id"]
        payload: dict[str, Any] = {"page_id": page_id, "fields": fields}
        # The whole page as it was before the session's first write to it.
        if (call.session_id, page_id) not in self._recorded:
            self._recorded.add((call.session_id, page_id))
            payload["snapshot"] = record(before.state["snapshot"])
        return Inverse(kind=INVERSE_KIND, payload=payload)

    # --- undo ---------------------------------------------------------------------------------

    async def plan_revert(self, action: Action) -> RewindStep:
        return await self._revert(action, apply=False)

    async def revert(self, action: Action) -> RewindStep:
        return await self._revert(action, apply=True)

    def _step(
        self,
        action: Action,
        changes: list[FieldChange],
        result: StepResult,
        detail: str | None,
    ) -> RewindStep:
        kept_first = sorted(
            changes, key=lambda c: c.outcome is not FieldChangeOutcome.keep_human_edit
        )
        return RewindStep(
            action_id=action.id,
            system=action.system,
            tool=action.tool,
            result=result,
            kept_fields=[
                c.field for c in kept_first if c.outcome is FieldChangeOutcome.keep_human_edit
            ],
            changes=kept_first,
            changes_truncated=False,
            detail=detail,
        )

    async def _revert(self, action: Action, *, apply: bool) -> RewindStep:
        inverse = action.inverse
        if inverse is None or not inverse.payload.get("fields"):
            return self._step(action, [], StepResult.skipped, "Nothing to undo.")
        if inverse.kind != INVERSE_KIND:
            return self._step(action, [], StepResult.failed, f"Unknown inverse {inverse.kind}.")
        page_id: str = inverse.payload["page_id"]
        fields: list[dict[str, Any]] = inverse.payload["fields"]
        changes: list[FieldChange] = []
        done: Counter[str] = Counter()
        agent_hashes = dict(action.after_hashes)

        def decide(field: dict[str, Any], current: str, shown: Any) -> bool:
            """Record the field's outcome. True if it still holds what the agent left."""
            agent = agent_hashes.get(field["key"])
            ok = current in (agent, before_hash(field))
            changes.append(
                FieldChange(
                    field=field["key"],
                    before=field["before_value"],
                    agent_value=field["agent_value"],
                    current=shown,
                    outcome=_outcome(ok),
                )
            )
            # Already back to its old value (or gone, for an appended block): nothing to write.
            return current == agent and current != before_hash(field)

        try:
            page = await self.client.get_page(page_id)
            in_trash = bool(page.get("in_trash"))
            for f in (f for f in fields if f["kind"] == "trash"):
                if decide(f, trash_hash(in_trash), in_trash):
                    done["untrash"] += 1
                    if apply:
                        page = await self.client.update_page(page_id, in_trash=f["before"])
                        in_trash = bool(page.get("in_trash"))
            rest = [f for f in fields if f["kind"] != "trash"]
            if rest and apply and in_trash:
                detail = (
                    "The page is in the trash, so its content can't be restored. Take it out of"
                    " the trash and rewind again."
                )
                return self._step(action, changes, StepResult.failed, detail)
            for f in [f for f in rest if f["kind"] == "append_lookup"]:
                rest.remove(f)
                # A trashed page's children can't be listed; only a dry run gets here with one.
                for new in [] if in_trash else await self._find_appended(page_id, f):
                    field = appended_field(page_id, new)
                    agent_hashes[field["key"]] = block_hash(new)  # it holds what was sent
                    rest.append(field)

            restore: dict[str, Any] = {}
            for f in (f for f in rest if f["kind"] == "property"):
                current = prop_form(page.get("properties", {}).get(f["name"]))
                if decide(f, form_hash(current), prop_value(current)):
                    restore[f["name"]] = restore_prop(f["before"])
            if restore and apply:
                await self.client.update_page(page_id, properties=restore)
            done["property"] += len(restore)

            for f in (f for f in rest if f["kind"] == "block"):
                blk = await self._block(f["block_id"])
                h = block_hash(blk, in_trash)
                shown = form_text(block_form(blk)) if blk is not None and h != ABSENT else None
                if decide(f, h, shown):
                    done["block"] += 1
                    if apply:
                        await self.client.update_block(f["block_id"], **restore_body(f["before"]))

            for f in reversed([f for f in rest if f["kind"] == "appended"]):
                blk = await self._block(f["block_id"])
                h = block_hash(blk, in_trash)
                shown = form_text(block_form(blk)) if blk is not None and h != ABSENT else None
                if decide(f, h, shown):
                    done["appended"] += 1
                    if apply:
                        await self.client.delete_block(f["block_id"])
        except Errors as exc:
            return self._step(action, changes, StepResult.failed, str(exc)[:500])
        finally:
            if apply:
                self._cache = None

        kept = sum(c.outcome is FieldChangeOutcome.keep_human_edit for c in changes)
        detail = _describe(done, kept, past=apply)
        if not apply:
            return self._step(action, changes, StepResult.pending, detail)
        result = StepResult.kept_human_edit if kept else StepResult.restored
        return self._step(action, changes, result, detail)

    async def _find_appended(self, page_id: str, f: dict[str, Any]) -> list[dict[str, Any]]:
        """The blocks an append that got no answer left, if it landed: blocks under the page
        that weren't there before, matching what was sent, in order."""
        known = set(f["before_ids"])
        sent: list[str] = f["sent"]
        found: list[dict[str, Any]] = []
        for blk in await self.client.children(page_id):
            if len(found) == len(sent):
                break
            if blk["id"] not in known and match_hash(block_form(blk)) == sent[len(found)]:
                found.append(blk)
        return found

    # --- checksums ----------------------------------------------------------------------------

    async def checksum(self) -> str | None:
        """Normalized hash of the configured pricing page (see ``canonical_hash``)."""
        if not self.default_page_id:
            return None
        try:
            return canonical_hash(await self.snapshot(self.default_page_id))
        except (ToolError, *Errors) as exc:
            log.warning("notion checksum failed: %s", exc)
            return None

    async def verify_scope(
        self, actions: list[Action], steps: list[RewindStep]
    ) -> tuple[str, str] | None:
        """(expected, actual) over exactly the properties, blocks and trash states the actions
        wrote. Each should be back to what it was before the oldest of them, unless the rewind
        kept a human's edit there, in which case it should still hold that edit."""
        kept = {
            c.field
            for s in steps
            for c in s.changes
            if c.outcome is FieldChangeOutcome.keep_human_edit
        }
        first: dict[str, tuple[str, dict[str, Any]]] = {}
        for action in sorted(actions, key=lambda a: a.created_at):
            inv = action.inverse
            if inv is None or inv.kind != INVERSE_KIND:
                continue
            for f in inv.payload.get("fields", []):
                first.setdefault(f["key"], (inv.payload["page_id"], f))
        pages: dict[str, dict[str, Any]] = {}
        expected: list[list[str]] = []
        actual: list[list[str]] = []
        for key, (page_id, f) in sorted(first.items()):
            if page_id not in pages:
                pages[page_id] = await self.client.get_page(page_id)
            page = pages[page_id]
            in_trash = bool(page.get("in_trash"))
            if f["kind"] == "trash":
                current = trash_hash(in_trash)
            elif f["kind"] == "property":
                current = form_hash(prop_form(page.get("properties", {}).get(f["name"])))
            elif f["kind"] == "append_lookup":
                # Back to before means none of the blocks it may have added are left.
                found = [] if in_trash else await self._find_appended(page_id, f)
                actual.append([key, sha([b["id"] for b in found]) if found else ABSENT])
                expected.append([key, ABSENT])
                continue
            else:
                current = block_hash(await self._block(f["block_id"]), in_trash)
            actual.append([key, current])
            expected.append([key, current if key in kept else before_hash(f)])
        return sha(expected), sha(actual)

    # --- demo ---------------------------------------------------------------------------------

    async def reset(self) -> None:
        """Demo only: put the pricing page back to demo/seed/notion_pricing.json, keeping the
        seeded block ids (a block that is gone for good is recreated, with a new id)."""
        if not self.default_page_id:
            raise RuntimeError("NOTION_PRICING_PAGE_ID is not set, so Notion can't be reset")
        seed = json.loads(self.seed_path.read_text())  # noqa: ASYNC240
        page_id = self.default_page_id
        try:
            page = await self.client.get_page(page_id)
            if page.get("in_trash"):
                page = await self.client.update_page(page_id, in_trash=False)
            props = {
                name: restore_prop(form)
                for name, form in seed["properties"].items()
                if prop_form(page["properties"].get(name)) != form
            }
            if props:
                await self.client.update_page(page_id, properties=props)
            await self._fix(page_id, seed["blocks"])
        finally:
            self._cache = None

    async def _fix(self, parent_id: str, nodes: list[dict[str, Any]]) -> None:
        wanted = {n["id"] for n in nodes}
        for b in await self.client.children(parent_id):
            if b["id"] not in wanted:
                await self.client.delete_block(b["id"])
        ids: list[str] = []
        for n in nodes:
            form = {"type": n["type"], "body": n["body"]}
            blk = await self._block(n["id"])
            parent = blk["parent"] if blk else {}
            if blk is None or parent_id not in parent.values() or blk["type"] != n["type"]:
                if blk is not None and not blk.get("in_trash"):
                    await self.client.delete_block(n["id"])
                new_id = await self._create(parent_id, n)
                log.warning("notion reset recreated block %s as %s", n["id"], new_id)
                ids.append(new_id)
                continue
            if blk.get("in_trash"):
                await self.client.update_block(n["id"], in_trash=False)
            if block_form(blk) != form:
                await self.client.update_block(n["id"], **restore_body(form))
            ids.append(n["id"])
        await self._reorder(parent_id, ids)
        for n, block_id in zip(nodes, ids, strict=True):
            if n["children"] and block_id == n["id"]:
                await self._fix(block_id, n["children"])

    async def _create(self, parent_id: str, node: dict[str, Any]) -> str:
        """Recreate a seed node and its subtree at the end of its parent. Returns the new id."""
        inline = node["type"] in INLINE_CHILDREN
        new = await self.client.append(parent_id, [_create_payload(node, inline)])
        new_id: str = new[0]["id"]
        if node["children"] and not inline:
            for child in node["children"]:
                await self._create(new_id, child)
        return new_id

    async def _reorder(self, parent_id: str, desired: list[str]) -> None:
        """There is no move endpoint, but untrash re-adds a block at the end of its parent. So
        trash every block from the first one out of place, then untrash them in order. Ids
        are kept."""
        current = [b["id"] for b in await self.client.children(parent_id)]
        if current == desired:
            return
        i = next(
            (k for k, (c, d) in enumerate(zip(current, desired, strict=False)) if c != d),
            min(len(current), len(desired)),
        )
        tail = desired[i:]
        for block_id in current[i:]:
            if block_id in tail:
                await self.client.delete_block(block_id)
        for block_id in tail:
            await self.client.update_block(block_id, in_trash=False)


def _create_payload(node: dict[str, Any], with_children: bool) -> dict[str, Any]:
    t = node["type"]
    body = dict(node["body"])
    if t == "synced_block":
        body.setdefault("synced_from", None)  # an original synced block sends an explicit null
    if with_children and node["children"]:
        body["children"] = [_create_payload(c, True) for c in node["children"]]
    return {"object": "block", "type": t, t: body}


def _outcome(ok: bool) -> FieldChangeOutcome:
    return FieldChangeOutcome.restore if ok else FieldChangeOutcome.keep_human_edit


def _describe(done: Counter[str], kept: int, *, past: bool) -> str:
    verbs = ("untrashed", "restored", "deleted", "kept")
    if not past:
        verbs = ("untrash", "restore", "delete", "keep")
    parts = []
    if done["untrash"]:
        parts.append(f"{verbs[0]} the page")
    restored = []
    if done["property"]:
        restored.append(_plural(done["property"], "property"))
    if done["block"]:
        restored.append(_plural(done["block"], "block"))
    if restored:
        parts.append(f"{verbs[1]} {' and '.join(restored)}")
    if done["appended"]:
        parts.append(f"{verbs[2]} {_plural(done['appended'], 'appended block')}")
    if kept:
        parts.append(f"{verbs[3]} {_plural(kept, 'human edit')}")
    if not parts:
        return "Nothing to restore."
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


__all__ = ["NotionAdapter", "ToolError", "canonical_hash", "record"]
