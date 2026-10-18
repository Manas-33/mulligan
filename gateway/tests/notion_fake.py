"""An in-memory Notion API (2026-03-11) behind httpx.MockTransport, seeded with the demo pricing
page. It echoes rich text the way Notion does (plain_text, default annotations, null link), keeps
child order, and treats a trashed page like Notion: its children can't be listed, its blocks read
``in_trash: true`` and can't be edited, and untrash re-adds a block at the end of its parent."""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from mulligan.adapters.notion.client import NotionClient

SEED = Path(__file__).resolve().parents[2] / "demo" / "seed" / "notion_pricing.json"
PAGE_ID = "11111111-2222-4333-8444-555555555555"
PARENT_ID = "66666666-7777-4888-8999-000000000000"
ANNOTATIONS = {
    "bold": False,
    "italic": False,
    "strikethrough": False,
    "underline": False,
    "code": False,
    "color": "default",
}


def echo_rich_text(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for item in items:
        content = item["text"]["content"]
        out.append(
            {
                "type": "text",
                "text": {"content": content, "link": item["text"].get("link")},
                "annotations": {**ANNOTATIONS, **item.get("annotations", {})},
                "plain_text": content,
                "href": None,
            }
        )
    return out


def echo_body(t: str, body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body.pop("children", None)
    if "rich_text" in body:
        body["rich_text"] = echo_rich_text(body["rich_text"])
    if "cells" in body:
        body["cells"] = [echo_rich_text(c) for c in body["cells"]]
    if t in {"paragraph", "bulleted_list_item", "numbered_list_item", "quote", "to_do"}:
        body.setdefault("color", "default")
    if t == "paragraph":
        body.setdefault("icon", None)
    return body


class FakeNotion:
    def __init__(self, seed: Path = SEED) -> None:
        data = json.loads(seed.read_text())
        self.page_id = PAGE_ID
        self.page: dict[str, Any] = {
            "object": "page",
            "id": self.page_id,
            "parent": {"type": "page_id", "page_id": PARENT_ID},
            "in_trash": False,
            "icon": None,
            "cover": None,
            "properties": {},
        }
        for name, form in data["properties"].items():
            self.page["properties"][name] = self._echo_prop(name, form["type"], form[form["type"]])
        self.blocks: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[str]] = {self.page_id: []}
        self._load(self.page_id, data["blocks"], "page_id")
        self.log: list[tuple[str, str]] = []
        self.fail: Callable[[httpx.Request], httpx.Response | None] | None = None

    def _load(self, parent: str, nodes: list[dict[str, Any]], kind: str) -> None:
        for n in nodes:
            self._add(parent, n["id"], n["type"], n["body"], kind)
            if n["children"]:
                self._load(n["id"], n["children"], "block_id")

    def _add(self, parent: str, block_id: str, t: str, body: dict[str, Any], kind: str) -> None:
        self.blocks[block_id] = {
            "object": "block",
            "id": block_id,
            "parent": {"type": kind, kind: parent},
            "has_children": False,
            "in_trash": False,
            "type": t,
            t: echo_body(t, body),
        }
        self.children.setdefault(parent, []).append(block_id)
        self.children.setdefault(block_id, [])
        if parent in self.blocks:
            self.blocks[parent]["has_children"] = True

    def _echo_prop(self, name: str, t: str, value: Any) -> dict[str, Any]:
        if t in {"title", "rich_text"}:
            value = echo_rich_text(value)
        elif t == "select" and value is not None:
            value = {"id": "opt1", "name": value["name"], "color": "default"}
        return {"id": "title" if t == "title" else name.lower(), "type": t, t: value}

    # --- helpers for tests --------------------------------------------------------------------

    def client(self) -> NotionClient:
        return NotionClient(
            "test-token", transport=httpx.MockTransport(self.handle), min_interval_s=0
        )

    def ids(self, parent: str | None = None) -> list[str]:
        return [b for b in self.children[parent or self.page_id] if not self.blocks[b]["in_trash"]]

    def text(self, block_id: str) -> str:
        b = self.blocks[block_id]
        body = b[b["type"]]
        if "cells" in body:
            return " | ".join("".join(x["plain_text"] for x in c) for c in body["cells"])
        return "".join(x["plain_text"] for x in body.get("rich_text", []))

    def find(self, text: str) -> str:
        return next(i for i in self.blocks if self.text(i) == text)

    def human_edit(self, block_id: str, text: str) -> None:
        """A person editing a block in Notion, not through Mulligan."""
        b = self.blocks[block_id]
        rt = [{"type": "text", "text": {"content": text}}]
        if b["type"] == "table_row":
            cells = [[{"type": "text", "text": {"content": c.strip()}}] for c in text.split("|")]
            b["table_row"]["cells"] = [echo_rich_text(c) for c in cells]
        else:
            b[b["type"]]["rich_text"] = echo_rich_text(rt)

    # --- the API ------------------------------------------------------------------------------

    def _page_trashed(self, block_id: str) -> bool:
        while block_id in self.blocks:
            parent = self.blocks[block_id]["parent"]
            block_id = parent.get("page_id") or parent.get("block_id")
        return block_id == self.page_id and bool(self.page["in_trash"])

    def _block_out(self, block_id: str) -> dict[str, Any]:
        b = copy.deepcopy(self.blocks[block_id])
        if self._page_trashed(block_id):
            b["in_trash"] = True
        return b

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.log.append((request.method, request.url.path))
        if self.fail:
            failed = self.fail(request)
            if failed is not None:
                return failed
        parts = request.url.path.removeprefix("/v1/").split("/")
        body = json.loads(request.content) if request.content else {}
        try:
            return httpx.Response(200, json=self._route(request.method, parts, body, request))
        except LookupError:
            return httpx.Response(404, json={"code": "object_not_found", "message": "not found"})
        except ValueError as exc:
            return httpx.Response(400, json={"code": "validation_error", "message": str(exc)})

    def _route(
        self, method: str, parts: list[str], body: dict[str, Any], request: httpx.Request
    ) -> dict[str, Any]:
        kind, oid = parts[0], parts[1]
        if kind == "pages":
            if oid != self.page_id:
                raise LookupError(oid)
            if method == "PATCH":
                if "in_trash" in body:
                    self.page["in_trash"] = body["in_trash"]
                for name, value in body.get("properties", {}).items():
                    t = self.page["properties"][name]["type"]
                    self.page["properties"][name] = self._echo_prop(name, t, value[t])
            return copy.deepcopy(self.page)
        if len(parts) == 3:  # children
            if oid == self.page_id and self.page["in_trash"]:
                raise LookupError(oid)
            if method == "GET":
                ids = [b for b in self.children[oid] if not self.blocks[b]["in_trash"]]
                return {"results": [self._block_out(b) for b in ids], "has_more": False}
            if self._page_trashed(oid) or (oid == self.page_id and self.page["in_trash"]):
                raise ValueError("can't edit a block in a trashed page")
            new = []
            after = (body.get("position") or {}).get("after_block", {}).get("id")
            for child in body["children"]:
                block_id = str(uuid.uuid4())
                kind_ = "page_id" if oid == self.page_id else "block_id"
                self._add(oid, block_id, child["type"], child[child["type"]], kind_)
                if after:
                    order = self.children[oid]
                    order.remove(block_id)
                    order.insert(order.index(after) + 1, block_id)
                    after = block_id
                new.append(self._block_out(block_id))
            return {"results": new}
        if oid not in self.blocks:
            raise LookupError(oid)
        b = self.blocks[oid]
        if method == "GET":
            return self._block_out(oid)
        if self._page_trashed(oid):
            raise ValueError("can't edit a block in a trashed page")
        if method == "DELETE":
            b["in_trash"] = True
            return self._block_out(oid)
        if body.get("in_trash") is False and b["in_trash"]:
            b["in_trash"] = False
            parent = b["parent"].get("page_id") or b["parent"].get("block_id")
            self.children[parent].remove(oid)
            self.children[parent].append(oid)  # untrash re-adds at the end
        t = b["type"]
        if t in body:
            if set(body) - {t, "in_trash"}:
                raise ValueError("can't change a block's type")
            update = body[t]
            if t == "table_row" and len(update.get("cells", [])) != len(b[t]["cells"]):
                raise ValueError("cell count must match the table width")
            b[t].update(echo_body(t, update))
        return self._block_out(oid)
