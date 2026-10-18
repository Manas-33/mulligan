"""Canonical forms of Notion properties and blocks: what Mulligan hashes, stores and restores.

A block's form is ``{type, body}``: its type's object as the API returns it, minus children, with
derived fields (``plain_text``, ``href``) and nulls dropped. A property's form is
``{type, <type>: value}`` the same way. Both are also valid request bodies, so restoring one is
sending it back. Forms are hashed as sorted, compact JSON; anything absent (a deleted block, a
missing property) hashes as sha256('').
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ABSENT = hashlib.sha256(b"").hexdigest()
DROP_KEYS = frozenset({"plain_text", "href"})
READ_ONLY_PROPS = frozenset(
    {
        "formula",
        "rollup",
        "created_time",
        "created_by",
        "last_edited_time",
        "last_edited_by",
        "unique_id",
        "verification",
        "button",
    }
)
TEXT_PROPS = frozenset({"title", "rich_text"})
SCALAR_PROPS = frozenset({"number", "checkbox", "url", "email", "phone_number"})
NAMED_PROPS = frozenset({"select", "status"})
APPEND_TYPES = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "callout",
        "code",
    }
)
# Blocks whose children are separate pages or databases, not part of this page's tree.
OWN_PAGES = frozenset({"child_page", "child_database"})
CELL_SEP = " | "
MAX_TEXT = 2000  # characters per rich text item


class ToolError(Exception):
    """Bad tool input. The agent sees this message."""


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if v is not None and k not in DROP_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def sha(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def seg(s: str) -> str:
    """One field-path segment ('%', '/' and ',' percent-encoded, as for Postgres)."""
    return s.replace("%", "%25").replace("/", "%2F").replace(",", "%2C")


# --- blocks -----------------------------------------------------------------------------------


def block_form(block: dict[str, Any]) -> dict[str, Any]:
    t = block["type"]
    body = clean(block.get(t) or {})
    body.pop("children", None)
    return {"type": t, "body": body}


def block_hash(block: dict[str, Any] | None, page_in_trash: bool = False) -> str:
    """A trashed block hashes as absent, except while its page is in the trash: then every block
    on the page reads ``in_trash: true`` and comes back with the page."""
    if block is None or (block.get("in_trash") and not page_in_trash):
        return ABSENT
    return sha(block_form(block))


def form_hash(form: dict[str, Any] | None) -> str:
    return ABSENT if form is None else sha(form)


def rich_text(text: str) -> list[dict[str, Any]]:
    chunks = [text[i : i + MAX_TEXT] for i in range(0, len(text), MAX_TEXT)] or [""]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def rt_text(items: list[dict[str, Any]] | None) -> str:
    out = []
    for item in items or []:
        text = item.get("plain_text")
        if text is None:
            text = (item.get("text") or {}).get("content")
        if text is None:
            text = (item.get("equation") or {}).get("expression", "")
        out.append(str(text))
    return "".join(out)


def form_text(form: dict[str, Any] | None) -> str | None:
    """The plain text an agent sees for a block form (cells joined with ' | ' for table rows)."""
    if form is None:
        return None
    body = form["body"]
    if "rich_text" in body:
        return rt_text(body["rich_text"])
    if form["type"] == "table_row":
        return CELL_SEP.join(rt_text(cell) for cell in body.get("cells", []))
    if form["type"] == "equation":
        return str(body.get("expression", ""))
    if "title" in body:
        return str(body["title"])
    return ""


def text_update(form: dict[str, Any], text: str) -> dict[str, Any]:
    """The request that replaces a block's plain text, keeping its type and everything else."""
    t, body = form["type"], form["body"]
    if "rich_text" in body:
        return {t: {"rich_text": rich_text(text)}}
    if t == "table_row":
        cells = text.split(CELL_SEP.strip())
        width = len(body.get("cells", []))
        if len(cells) != width:
            raise ToolError(f"table row needs {width} cells separated by '|', got {len(cells)}")
        return {t: {"cells": [rich_text(c.strip()) for c in cells]}}
    raise ToolError(f"a {t} block has no text to replace")


def restore_body(form: dict[str, Any]) -> dict[str, Any]:
    """The update request that puts a block back to ``form``."""
    t = form["type"]
    body = dict(form["body"])
    body.pop("table_width", None)  # fixed when the table is created
    return {t: body}


def append_payload(item: dict[str, Any]) -> dict[str, Any]:
    t = str(item.get("type", ""))
    if t not in APPEND_TYPES:
        raise ToolError(f"can't append a {t!r} block; use one of {', '.join(sorted(APPEND_TYPES))}")
    body: dict[str, Any] = {"rich_text": rich_text(str(item.get("text", "")))}
    if t == "to_do":
        body["checked"] = False
    if t == "code":
        body["language"] = "plain text"
    return {"object": "block", "type": t, t: body}


# --- properties -------------------------------------------------------------------------------


def prop_form(prop: dict[str, Any] | None) -> dict[str, Any] | None:
    if prop is None:
        return None
    t = prop["type"]
    form: dict[str, Any] = {"type": t}
    if prop.get(t) is not None:
        form[t] = clean(prop[t])
    return form


def prop_value(form: dict[str, Any] | None) -> Any:
    """A property as the simplified JSON agents read and write (contracts/mcp_tools.md)."""
    if form is None:
        return None
    t = form["type"]
    v = form.get(t)
    if t in TEXT_PROPS:
        return rt_text(v)
    if v is None:
        return [] if t in {"multi_select", "people", "relation", "files"} else None
    if t in SCALAR_PROPS or t in {"created_time", "last_edited_time"}:
        return v
    if t in NAMED_PROPS:
        return v.get("name")
    if t == "multi_select":
        return [o.get("name") for o in v]
    if t == "date":
        return v.get("start") if not v.get("end") else f"{v['start']}/{v['end']}"
    if t == "people":
        return [p.get("name") or p.get("id") for p in v]
    if t == "relation":
        return [r.get("id") for r in v]
    if t == "files":
        return [f.get("name") for f in v]
    if t in {"created_by", "last_edited_by"}:
        return v.get("name") or v.get("id")
    if t in {"formula", "rollup"} and isinstance(v, dict):
        return v.get(v.get("type", ""))
    if t == "unique_id":
        return f"{v.get('prefix') or ''}{'-' if v.get('prefix') else ''}{v.get('number')}"
    return v


def prop_request(t: str, value: Any) -> Any:
    """A simplified value as the request value for a property of type ``t``."""
    if t in TEXT_PROPS:
        return rich_text("" if value is None else str(value))
    if t in SCALAR_PROPS:
        if t == "number" and value is not None and not isinstance(value, int | float):
            raise ToolError("a number property needs a number")
        if t == "checkbox" and not isinstance(value, bool):
            raise ToolError("a checkbox property needs true or false")
        return value
    if t in NAMED_PROPS:
        return None if value in (None, "") else {"name": str(value)}
    if t == "multi_select":
        if not isinstance(value, list):
            raise ToolError("a multi_select property needs a list of names")
        return [{"name": str(v)} for v in value]
    if t == "date":
        if value in (None, ""):
            return None
        start, _, end = str(value).partition("/")
        return {"start": start, **({"end": end} if end else {})}
    raise ToolError(f"Mulligan can't set a {t} property")


def restore_prop(form: dict[str, Any]) -> dict[str, Any]:
    """The update request value that puts a property back to ``form``."""
    t = form["type"]
    v = form.get(t)
    if t in NAMED_PROPS and v is not None:
        v = {"name": v["name"]} if "name" in v else {"id": v["id"]}
    elif t == "multi_select" and v is not None:
        v = [{"name": o["name"]} if "name" in o else {"id": o["id"]} for o in v]
    elif t in {"people", "relation"}:
        v = [{"id": o["id"]} for o in v or []]
    elif t in TEXT_PROPS or t in {"files"}:
        v = v or []
    return {t: v}


def title_of(page: dict[str, Any]) -> str:
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return rt_text(prop.get("title"))
    return ""
