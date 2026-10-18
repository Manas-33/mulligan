"""Spike 0.4: Notion restore. Throwaway; nothing outside spikes/ may import this.

    python spike.py run     # criterion 1 (full restore) + criterion 2 (teammate edit kept and flagged)
    python spike.py probe   # which block types survive a read -> recreate round trip
    python spike.py reset   # put the page back to .state/original.json

Credentials come from the repo-root .env (NOTION_TOKEN, NOTION_PRICING_PAGE_ID).
"""

import copy
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from notion_client import APIResponseError, Client

NOTION_VERSION = "2026-03-11"
HERE = Path(__file__).resolve().parent
STATE = HERE / ".state"


def _repo_env() -> Path:
    # In a worktree the .env lives in the main checkout, next to the shared .git dir.
    common = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=HERE, capture_output=True, text=True, check=True,
    ).stdout.strip()
    return Path(common).parent / ".env"


load_dotenv(HERE.parents[1] / ".env")
load_dotenv(_repo_env())
# The env var holds the page URL. Use the dashed form: the API returns ids dashed (e.g. in
# block.parent.page_id), so an undashed id never compares equal to them.
PAGE_ID = str(uuid.UUID(re.findall(r"[0-9a-f]{32}", os.environ["NOTION_PRICING_PAGE_ID"].replace("-", ""))[-1]))
notion = Client(auth=os.environ["NOTION_TOKEN"], notion_version=NOTION_VERSION)

# Count requests and rate-limit retries without logging bodies or headers.
STATS = {"requests": 0, "retries": 0}
_request = notion.request


def _counted(*a, **kw):
    STATS["requests"] += 1
    return _request(*a, **kw)


notion.request = _counted


class _RetryCounter(logging.Handler):
    def emit(self, record):
        if "retry" in record.getMessage().lower() or "rate" in record.getMessage().lower():
            STATS["retries"] += 1


logging.getLogger("notion_client").addHandler(_RetryCounter())


# ---------------------------------------------------------------- normalize / hash

DROP_KEYS = {"plain_text", "href"}  # derived, read-only fields inside rich text
READ_ONLY_PROPS = {"formula", "rollup", "created_time", "created_by", "last_edited_time",
                   "last_edited_by", "unique_id", "verification", "button"}


def clean(v):
    """Response shape -> request shape. Also the canonical form we hash (None == absent)."""
    if isinstance(v, dict):
        return {k: clean(x) for k, x in v.items() if x is not None and k not in DROP_KEYS}
    if isinstance(v, list):
        return [clean(x) for x in v]
    return v


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()[:16]


def block_state(b: dict) -> dict:
    """One block, no children: what the agent wrote and what we compare against later."""
    return {"type": b["type"], "body": clean(b[b["type"]]), "in_trash": b.get("in_trash", False)}


def prop_state(page: dict, name: str) -> dict:
    p = page["properties"][name]
    return {"type": p["type"], p["type"]: clean(p[p["type"]])}


def tree_norm(nodes: list) -> list:
    return [{"type": n["type"], "body": clean(n["body"]), "children": tree_norm(n["children"])}
            for n in nodes]


def page_hash(snap: dict) -> str:
    return sha({"page": snap["page"], "blocks": tree_norm(snap["blocks"])})


# ---------------------------------------------------------------- read

def list_children(block_id: str) -> list:
    out, cursor = [], None
    while True:
        kw = {"page_size": 100, **({"start_cursor": cursor} if cursor else {})}
        r = notion.blocks.children.list(block_id, **kw)
        out += r["results"]
        if not r["has_more"]:
            return out
        cursor = r["next_cursor"]


def read_tree(block_id: str) -> list:
    nodes = []
    for b in list_children(block_id):
        recurse = b["has_children"] and b["type"] not in ("child_page", "child_database")
        nodes.append({"id": b["id"], "type": b["type"], "body": b[b["type"]],
                      "children": read_tree(b["id"]) if recurse else []})
    return nodes


def snapshot() -> dict:
    p = notion.pages.retrieve(PAGE_ID)
    props = {k: prop_state(p, k) for k, v in p["properties"].items() if v["type"] not in READ_ONLY_PROPS}
    return {"page": {"in_trash": p["in_trash"], "icon": clean(p["icon"]), "cover": clean(p["cover"]),
                     "properties": props},
            # A trashed page's children 404 (object_not_found), so there is nothing to read.
            "blocks": [] if p["in_trash"] else read_tree(PAGE_ID)}


def flat(nodes: list) -> list:
    out = []
    for n in nodes:
        out.append(n)
        out += flat(n["children"])
    return out


# ---------------------------------------------------------------- write helpers

def create_payload(node: dict, with_children: bool) -> dict:
    t, body = node["type"], clean(node["body"])
    if t == "synced_block":
        body.setdefault("synced_from", None)  # an original synced block must send an explicit null
    if with_children and node["children"]:
        body["children"] = [create_payload(c, True) for c in node["children"]]
    return {"object": "block", "type": t, t: body}


def update_payload(state: dict) -> dict:
    t, body = state["type"], copy.deepcopy(state["body"])
    body.pop("table_width", None)  # fixed at creation
    body.pop("children", None)
    return {t: body}


def position(after_id):
    return {"type": "after_block", "after_block": {"id": after_id}} if after_id else {"type": "start"}


def create_block(parent_id: str, node: dict, after_id) -> str:
    """Recreate one snapshot node (and its subtree) right after after_id. Returns the new id."""
    # Tables, column lists and tabs must ship their children inline; everything else gets its
    # children appended after, since one append request only takes two levels of nesting.
    inline = node["type"] in ("table", "column_list", "column", "tab")
    r = notion.blocks.children.append(parent_id, children=[create_payload(node, inline)],
                                      position=position(after_id))
    new_id = r["results"][0]["id"]
    if node["children"] and not inline:
        prev = None
        for c in node["children"]:
            prev = create_block(new_id, c, prev)
    return new_id


def prev_sibling(block: dict):
    parent = block["parent"].get("page_id") or block["parent"].get("block_id")
    ids = [b["id"] for b in list_children(parent)]
    i = ids.index(block["id"])
    return parent, (ids[i - 1] if i else None)


# ---------------------------------------------------------------- the gateway's action log

class Recorder:
    """What Mulligan's Notion adapter would log: before-state plus a hash of the after-state."""

    def __init__(self):
        self.actions = []

    def update_block(self, block_id: str, payload: dict):
        before = notion.blocks.retrieve(block_id)
        after = notion.blocks.update(block_id, **payload)
        self.actions.append({"op": "update_block", "id": block_id, "before": block_state(before),
                             "after_hash": sha(block_state(after))})

    def delete_block(self, block_id: str):
        before = notion.blocks.retrieve(block_id)
        parent, prev = prev_sibling(before)
        node = {"id": block_id, "type": before["type"], "body": before[before["type"]],
                "children": read_tree(block_id) if before["has_children"] else []}
        after = notion.blocks.delete(block_id)
        self.actions.append({"op": "delete_block", "id": block_id, "parent": parent, "prev": prev,
                             "before": node, "after_hash": sha(block_state(after))})

    def append_block(self, parent_id: str, payload: dict, after_id=None):
        r = notion.blocks.children.append(parent_id, children=[payload], position=position(after_id))
        new = r["results"][0]
        self.actions.append({"op": "append_block", "id": new["id"], "parent": parent_id,
                             "after_hash": sha(block_state(new))})
        return new["id"]

    def update_props(self, props: dict):
        before = notion.pages.retrieve(PAGE_ID)
        after = notion.pages.update(PAGE_ID, properties=props)
        for name in props:
            self.actions.append({"op": "update_prop", "name": name, "before": prop_state(before, name),
                                 "after_hash": sha(prop_state(after, name))})

    def trash_page(self):
        before = notion.pages.retrieve(PAGE_ID)
        after = notion.pages.update(PAGE_ID, in_trash=True)
        self.actions.append({"op": "trash_page", "before": before["in_trash"],
                             "after_hash": sha(after["in_trash"])})


def current_hash(a: dict):
    try:
        if a["op"] in ("update_block", "delete_block", "append_block"):
            return sha(block_state(notion.blocks.retrieve(a["id"])))
        page = notion.pages.retrieve(PAGE_ID)
        return sha(prop_state(page, a["name"])) if a["op"] == "update_prop" else sha(page["in_trash"])
    except APIResponseError as e:  # gone for good: someone else deleted it
        return f"error:{e.code}"


def reorder(parent_id: str, desired: list) -> int:
    """There is no move-block endpoint, but untrash always re-adds a block at the END of its
    parent. So: trash everything from the first out-of-place block on, then untrash that tail in
    the desired order. Keeps every block id; costs 2 requests per block in the tail."""
    current = [b["id"] for b in list_children(parent_id)]
    if current == desired:
        return 0
    i = next((k for k, (c, d) in enumerate(zip(current, desired)) if c != d),
             min(len(current), len(desired)))
    tail = desired[i:]
    for bid in current[i:]:
        if bid in tail:
            notion.blocks.delete(bid)
    for bid in tail:
        notion.blocks.update(bid, in_trash=False)
    return len(tail)


def rewind(actions: list, force_recreate=False) -> dict:
    """Undo newest first. Anything whose current hash != the agent's after-hash was changed by a
    human since: keep it and flag it.

    Deleted blocks come back by untrash (same id) and are then moved into place with reorder(),
    or, with force_recreate, are recreated at their old position (new id)."""
    flagged, idmap, moved = [], {}, 0
    # Simulate each touched parent's child order through the undo, then apply it once at the end.
    # Read lazily: a trashed page's children 404 until the trash_page undo has run.
    order = {}

    def sim(parent_id):
        if parent_id not in order:
            order[parent_id] = [b["id"] for b in list_children(parent_id)]
        return order[parent_id]

    for a in reversed(actions):
        if current_hash(a) != a["after_hash"]:
            flagged.append(a)
            continue
        op = a["op"]
        if op == "trash_page":
            notion.pages.update(PAGE_ID, in_trash=a["before"])
        elif op == "update_prop":
            notion.pages.update(PAGE_ID, properties={a["name"]: a["before"]})
        elif op == "append_block":
            sim(a["parent"]).remove(a["id"])  # read the order before this delete changes it
            notion.blocks.delete(a["id"])
        elif op == "update_block":
            notion.blocks.update(a["id"], **update_payload(a["before"]))
        elif op == "delete_block":
            # Newest-first means the sibling that preceded it at delete time is back in place now.
            sib = sim(a["parent"])
            if force_recreate:
                anchor = idmap.get(a["prev"], a["prev"]) if a["prev"] else None
                new_id = create_block(a["parent"], a["before"], anchor)
                idmap[a["id"]] = new_id
            else:
                notion.blocks.update(a["id"], in_trash=False)  # lands at the end of the parent
                new_id = a["id"]
            sib.insert(sib.index(a["prev"]) + 1 if a["prev"] in sib else 0, new_id)
    if not force_recreate:
        moved = sum(reorder(p, ids) for p, ids in order.items())
    return {"flagged": flagged, "recreated": idmap, "moved": moved}


# ---------------------------------------------------------------- scenario

def text(s: str) -> list:
    return [{"type": "text", "text": {"content": s}}]


def rogue_edit(snap: dict, rec: Recorder, trash=True):
    """Rewrite several blocks the way a runaway pricing agent would."""
    by_text = {}
    for n in flat(snap["blocks"]):
        rt = n["body"].get("rich_text") or []
        by_text["".join(x["plain_text"] for x in rt)] = n
    tbl = next(n for n in snap["blocks"] if n["type"] == "table")
    intro = by_text["Prices for Acme Widgets plans, effective October 2026."]
    starter, pro = by_text["Starter: $29/month"], by_text["Pro: $99/month"]
    enterprise = by_text["Enterprise: $499/month"]
    todo = by_text["Review enterprise discount with sales"]
    basic_row = tbl["children"][1]

    rec.update_block(intro["id"], {"paragraph": {"rich_text": text("All prices doubled, effective now.")}})
    rec.update_block(pro["id"], {"bulleted_list_item": {"rich_text": text("Pro: $199/month")}})
    rec.update_block(basic_row["id"], {"table_row": {"cells": [text("Widget Basic"), text("$24")]}})
    rec.delete_block(enterprise["id"])
    rec.delete_block(todo["id"])
    rec.append_block(PAGE_ID, {"type": "bulleted_list_item",
                               "bulleted_list_item": {"rich_text": text("Starter Plus: $59/month")}},
                     after_id=starter["id"])
    rec.append_block(PAGE_ID, {"type": "paragraph",
                               "paragraph": {"rich_text": text("New: Ultra plan, $999/month.")}})
    rec.update_props({"title": {"title": text("Acme Widgets — Pricing (v2, doubled)")}})
    if trash:  # the API can't untrash a workspace-level page; this one must have a parent page
        rec.trash_page()
    return {"basic_row": basic_row, "heading": snap["blocks"][0]}


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def ids(snap: dict) -> list:
    return [n["id"] for n in flat(snap["blocks"])]


def run():
    STATE.mkdir(exist_ok=True)
    t0 = time.time()
    orig = snapshot()
    if not (STATE / "original.json").exists():  # first run only: the safety net for `reset`
        (STATE / "original.json").write_text(json.dumps(orig, indent=1, ensure_ascii=False))
    h0 = page_hash(orig)
    print(f"Notion-Version {NOTION_VERSION}; original page hash {h0}; "
          f"{len(flat(orig['blocks']))} blocks")
    results = {}

    print("\n1a. Rogue edit + page trash, then rewind (untrash deleted blocks)")
    rec = Recorder()
    rogue_edit(orig, rec)
    mid = snapshot()
    print(f"  after rogue: hash {page_hash(mid)}, in_trash={mid['page']['in_trash']}, "
          f"{len(rec.actions)} actions logged")
    out = rewind(rec.actions)
    now = snapshot()
    ok = check("normalized hash matches original", page_hash(now) == h0, page_hash(now))
    ok &= check("no false conflicts", not out["flagged"], f"{len(out['flagged'])} flagged")
    ok &= check("block ids identical to original (order + identity kept)", ids(now) == ids(orig),
                f"reorder moved {out['moved']} blocks")
    results["1a"] = ok

    print("\n1b. Same rogue edit, rewind by recreating deleted blocks (new ids, position.after_block)")
    rec = Recorder()
    rogue_edit(orig, rec)
    out = rewind(rec.actions, force_recreate=True)
    now = snapshot()
    ok = check("normalized hash matches original", page_hash(now) == h0, page_hash(now))
    ok &= check("recreated blocks landed in original order",
                [n["type"] for n in flat(now["blocks"])] == [n["type"] for n in flat(orig["blocks"])])
    print(f"  recreated {len(out['recreated'])} blocks with new ids")
    # Swap the originals back in so the page keeps its original block ids.
    for new in out["recreated"].values():
        notion.blocks.delete(new)
    moved = reorder(PAGE_ID, [n["id"] for n in orig["blocks"]])
    print(f"  swapped originals back via reorder ({moved} blocks trashed + untrashed)")
    now = snapshot()
    ok &= check("after swapping originals back: hash + ids match",
                page_hash(now) == h0 and ids(now) == ids(orig))
    results["1b"] = ok

    print("\n2. Rogue edit, teammate fixes a price the agent wrote, then rewind")
    rec = Recorder()
    hit = rogue_edit(orig, rec, trash=False)  # trashed below, after the teammate's edit
    row = hit["basic_row"]
    edited = notion.blocks.update(row["id"], table_row={"cells": [text("Widget Basic"), text("$15")]})
    teammate_cells = clean(edited["table_row"]["cells"])  # straight to Notion, not via Mulligan
    rec.trash_page()
    try:  # can a human still edit a block while its page is in the trash? (no-op write)
        h = hit["heading"]
        notion.blocks.update(h["id"], **update_payload({"type": h["type"], "body": clean(h["body"])}))
        print("  note: editing a block inside a trashed page is allowed")
    except APIResponseError as e:
        print(f"  note: editing a block inside a trashed page is refused ({e.code})")
    out = rewind(rec.actions)
    flagged_ids = [a.get("id") for a in out["flagged"]]
    expected = copy.deepcopy(orig)
    for n in flat(expected["blocks"]):
        if n["id"] == row["id"]:
            n["body"] = {**n["body"], "cells": teammate_cells}
    now = snapshot()
    ok = check("teammate's block flagged (and only it)", flagged_ids == [row["id"]],
               f"flagged {flagged_ids}")
    kept = next(n for n in flat(now["blocks"]) if n["id"] == row["id"])
    ok &= check("teammate's edit kept", clean(kept["body"]["cells"]) == clean(teammate_cells),
                "".join(c[0]["plain_text"] for c in kept["body"]["cells"][1:]))
    ok &= check("everything else matches original", page_hash(now) == page_hash(expected))
    results["2"] = ok

    print("\nCleanup: undo the teammate's edit so the page is back to its original state")
    orig_row = next(n for n in flat(orig["blocks"]) if n["id"] == row["id"])
    notion.blocks.update(row["id"], table_row={"cells": clean(orig_row["body"]["cells"])})
    final = snapshot()
    results["final"] = check("page back to original (hash + ids)",
                             page_hash(final) == h0 and ids(final) == ids(orig))

    print(f"\n{STATS['requests']} requests, {STATS['retries']} rate-limit/transient retries, "
          f"{time.time() - t0:.0f}s")
    print(f"\nCRITERION 1 (restored hash == original): {'PASS' if results['1a'] and results['1b'] else 'FAIL'}")
    print(f"CRITERION 2 (teammate edit kept + flagged, rest matches): {'PASS' if results['2'] else 'FAIL'}")
    print(f"PAGE RESTORED: {'PASS' if results['final'] else 'FAIL'}")
    return all(results.values())


# ---------------------------------------------------------------- block-type probe

def _probes():
    p = {"rich_text": text("probe")}
    ext = {"type": "external", "external": {"url": "https://www.notion.so/images/favicon.ico"}}
    col = {"object": "block", "type": "column",
           "column": {"children": [{"type": "paragraph", "paragraph": p}]}}
    return {
        "heading_4": p, "callout": {**p, "icon": {"type": "emoji", "emoji": "💡"}}, "quote": p,
        "code": {**p, "language": "python"}, "equation": {"expression": "e=mc^2"}, "divider": {},
        "toggle": {**p, "children": [{"type": "paragraph", "paragraph": p}]},
        "numbered_list_item": p, "table_of_contents": {}, "breadcrumb": {},
        "bookmark": {"url": "https://example.com"}, "embed": {"url": "https://example.com"},
        "image": ext, "link_to_page": {"type": "page_id", "page_id": PAGE_ID},
        "column_list": {"children": [col, col]},
        "synced_block": {"synced_from": None, "children": [{"type": "paragraph", "paragraph": p}]},
        "paragraph_with_mention": {"rich_text": [{"type": "mention", "mention": {
            "type": "date", "date": {"start": "2026-10-18"}}}]},
        "child_page": {"title": "probe"}, "link_preview": {"url": "https://github.com/Manas-33"},
        "meeting_notes": {}, "tab": {"children": [{"type": "paragraph", "paragraph": p}]}, "button": {}, "template": p,
        "paragraph_with_page_mention": {"rich_text": [{"type": "mention", "mention": {
            "type": "page", "page": {"id": PAGE_ID}}}]},
        "paragraph_with_user_mention": {"rich_text": [{"type": "mention", "mention": {
            "type": "user", "user": {"id": notion.users.me()["id"]}}}]},
    }


def probe():
    print(f"Notion-Version {NOTION_VERSION}: create -> read -> recreate -> read, compare\n")
    only = sys.argv[2:]
    for name, body in _probes().items():
        if only and name not in only:
            continue
        t = "paragraph" if name.startswith("paragraph_with") else name
        made = []
        try:
            r = notion.blocks.children.append(PAGE_ID, children=[{"type": t, t: body}])
            made.append(r["results"][0]["id"])
            first = read_tree(PAGE_ID)[-1]
            second_id = create_block(PAGE_ID, first, first["id"])
            made.append(second_id)
            nodes = read_tree(PAGE_ID)
            second = next(n for n in nodes if n["id"] == second_id)
            same = tree_norm([first]) == tree_norm([second])
            print(f"  {name:24} {'round-trips' if same else 'DIFFERS on recreate'}")
            if not same:
                print("     read:", json.dumps(tree_norm([first]))[:300])
                print("     redo:", json.dumps(tree_norm([second]))[:300])
        except APIResponseError as e:
            print(f"  {name:24} REFUSED ({e.code}: {str(e)[:120]})")
        finally:
            for bid in made:
                notion.blocks.delete(bid)
    print(f"\n{STATS['requests']} requests, {STATS['retries']} retries")


def reset():
    """Emergency: put the page back to .state/original.json, keeping the original block ids."""
    orig = json.loads((STATE / "original.json").read_text())
    if notion.pages.retrieve(PAGE_ID)["in_trash"]:
        notion.pages.update(PAGE_ID, in_trash=False)  # fails for workspace-level pages
    notion.pages.update(PAGE_ID, properties=orig["page"]["properties"])

    def fix(parent_id, nodes):
        want = [n["id"] for n in nodes]
        for b in list_children(parent_id):
            if b["id"] not in want:
                notion.blocks.delete(b["id"])
        for n in nodes:
            b = notion.blocks.retrieve(n["id"])
            if b["in_trash"]:
                notion.blocks.update(n["id"], in_trash=False)
            if clean(b[b["type"]]) != clean(n["body"]):
                notion.blocks.update(n["id"], **update_payload({"type": n["type"], "body": clean(n["body"])}))
        reorder(parent_id, want)
        for n in nodes:
            if n["children"]:
                fix(n["id"], n["children"])

    fix(PAGE_ID, orig["blocks"])
    now = snapshot()
    ok = page_hash(now) == page_hash(orig) and ids(now) == ids(orig)
    print("reset:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    sys.exit(0 if {"run": run, "probe": probe, "reset": reset}[cmd]() else 1)
