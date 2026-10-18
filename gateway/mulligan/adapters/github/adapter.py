"""The GitHub adapter: issue, comment and file tools on one repository, and conflict-aware undo.

Field paths (``Action.after_hashes``): ``github:issues/<n>/state`` (state and state_reason),
``github:comments/<id>`` (the body, or absent once deleted) and ``github:files/<path>`` (the blob
sha on the default branch; ``github:files/<path>@<branch>`` on another branch). Each hash is
sha256 over the value's canonical JSON; absent is sha256('').
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
from typing import Any, ClassVar

from mulligan.adapters.github.client import GitHubClient, GitHubError, content_path
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
from mulligan.settings import REPO_ROOT, Settings

ABSENT = hashlib.sha256(b"").hexdigest()
MOD = 2**256
TEXT_LIMIT = 4000  # characters of file text kept for the dashboard's diff
SEED_MODULE = REPO_ROOT / "demo" / "seed" / "github_seed.py"

WRITE_TOOLS = frozenset({"github_close_issue", "github_comment", "github_update_file"})


class ToolError(Exception):
    """A bad argument or a GitHub refusal, returned to the agent as a tool error."""


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


def load_seed() -> Any:
    """demo/seed/github_seed.py, which lives outside the package (the image copies demo/seed)."""
    name = "mulligan_demo_github_seed"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SEED_MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"no GitHub seed at {SEED_MODULE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while it loads
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def field_hash(value: Any) -> str:
    """sha256 of the canonical JSON of ``value``; ``MISSING`` (absent) hashes as sha256('')."""
    if value is MISSING:
        return ABSENT
    return hashlib.sha256(canon(value).encode()).hexdigest()


def seg(s: str) -> str:
    return s.replace("%", "%25").replace("/", "%2F").replace(",", "%2C")


def state_value(issue: dict[str, Any]) -> dict[str, Any]:
    """An issue's state as hashed: the reason only counts while it is closed (GitHub sets
    ``reopened`` on reopen, which an issue that was simply open never had)."""
    closed = issue.get("state") == "closed"
    return {
        "state": issue.get("state"),
        "state_reason": issue.get("state_reason") if closed else None,
    }


def issue_view(issue: dict[str, Any]) -> dict[str, Any]:
    """The fields Mulligan hashes, labels and assignees sorted."""
    return {
        "number": issue["number"],
        "state": state_value(issue),
        "title": issue.get("title") or "",
        "body": issue.get("body") or "",
        "labels": sorted(
            label["name"] if isinstance(label, dict) else str(label)
            for label in issue.get("labels") or []
        ),
        "assignees": sorted(user["login"] for user in issue.get("assignees") or []),
    }


def comment_issue(comment: dict[str, Any]) -> int:
    return int(str(comment.get("issue_url", "")).rstrip("/").rsplit("/", 1)[-1] or 0)


def _term(*parts: Any) -> int:
    return int(hashlib.sha256(canon(list(parts)).encode()).hexdigest(), 16)


def _text(item: dict[str, Any] | None) -> str | None:
    """A file's text from a contents response, if it is small UTF-8."""
    if not item or item.get("encoding") != "base64" or not item.get("content"):
        return None if not item or item.get("size") else ""
    try:
        text = base64.b64decode(item["content"]).decode()
    except (ValueError, UnicodeDecodeError):
        return None
    return text[:TEXT_LIMIT]


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


class GitHubAdapter(Adapter):
    system: ClassVar[System] = System.github
    tools: ClassVar[frozenset[str]] = WRITE_TOOLS | {"github_list_issues"}

    def __init__(self, client: GitHubClient, default_branch: str = "main") -> None:
        self.client = client
        self.default_branch = default_branch

    @classmethod
    async def open(cls, settings: Settings) -> GitHubAdapter:
        if not (settings.github_org and settings.github_repo):
            raise RuntimeError("GITHUB_ORG and GITHUB_REPO must be set with GITHUB_TOKEN")
        client = GitHubClient(
            settings.github_token,
            settings.github_org,
            settings.github_repo,
            write_interval=settings.github_write_interval_ms / 1000,
        )
        try:
            repo = await client.get("")
        except Exception:
            await client.aclose()
            raise
        return cls(client, str(repo.get("default_branch") or "main"))

    async def close(self) -> None:
        await self.client.aclose()

    async def reset(self) -> None:
        """Demo only: put the repository back to the Acme Widgets seed (demo/seed)."""
        await load_seed().reset(self.client)

    def file_key(self, path: str, branch: str) -> str:
        key = f"github:files/{seg(path)}"
        return key if branch == self.default_branch else f"{key}@{seg(branch)}"

    # --- executing ----------------------------------------------------------------------------

    async def capture_before(self, call: ToolCall) -> Capture:
        args = call.args
        if call.tool == "github_close_issue":
            issue = await self._issue(int(args["number"]))
            return Capture(ref=f"github:issues/{issue['number']}", state={"issue": issue})
        if call.tool == "github_comment":
            return Capture(ref=f"github:issues/{int(args['number'])}")
        if call.tool == "github_update_file":
            path = _clean_path(str(args["path"]))
            branch = str(args.get("branch") or self.default_branch)
            item = await self._file(path, branch)
            sha = item["sha"] if item else None
            return Capture(
                ref=f"github:blob/{sha}" if sha else None,
                state={"path": path, "branch": branch, "sha": sha, "text": _text(item)},
            )
        return Capture()

    async def execute(self, call: ToolCall, before: Capture) -> ToolResult:
        try:
            if call.tool == "github_list_issues":
                return await self._list(call.args)
            if call.tool == "github_close_issue":
                return await self._close(call.args, before)
            if call.tool == "github_comment":
                return await self._comment(call.args, before)
            if call.tool == "github_update_file":
                return await self._update_file(call.args, before)
        except (ToolError, GitHubError) as exc:
            return ToolResult(
                structured_content={"error": str(exc)}, summary=str(exc), is_error=True
            )
        raise ToolError(f"unknown tool {call.tool}")

    async def _issue(self, number: int) -> dict[str, Any]:
        issue = await self.client.get_or_none(f"issues/{number}")
        if issue is None:
            raise ToolError(f"Issue #{number} not found")
        if issue.get("pull_request"):
            raise ToolError(f"#{number} is a pull request, not an issue")
        result: dict[str, Any] = issue
        return result

    async def _file(self, path: str, branch: str) -> dict[str, Any] | None:
        item: dict[str, Any] | list[Any] | None = await self.client.get_or_none(
            content_path(path), {"ref": branch}
        )
        if isinstance(item, list) or (item and item.get("type") != "file"):
            raise ToolError(f"{path} is not a file")
        return item

    async def _list(self, args: dict[str, Any]) -> ToolResult:
        params: dict[str, Any] = {
            "state": args.get("state") or "open",
            "per_page": min(max(int(args.get("per_page") or 30), 1), 100),
            "page": max(int(args.get("page") or 1), 1),
        }
        if args.get("labels"):
            params["labels"] = ",".join(str(label) for label in args["labels"])
        if args.get("since"):
            params["since"] = str(args["since"])
        raw = await self.client.get("issues", params)
        issues = [
            {
                "number": i["number"],
                "title": i.get("title") or "",
                "state": i.get("state"),
                "labels": issue_view(i)["labels"],
                "assignees": issue_view(i)["assignees"],
                "comments": i.get("comments", 0),
                "updated_at": i.get("updated_at"),
            }
            for i in raw
            if not i.get("pull_request")
        ]
        return ToolResult(
            structured_content={"issues": issues},
            summary=f"Listed {_plural(len(issues), params['state'] + ' issue')}",
        )

    async def _close(self, args: dict[str, Any], before: Capture) -> ToolResult:
        number = int(args["number"])
        reason = str(args.get("state_reason") or "completed")
        if reason not in ("completed", "not_planned"):
            raise ToolError("state_reason must be completed or not_planned")
        comment = None
        if args.get("comment"):
            comment = await self.client.send(
                "POST", f"issues/{number}/comments", {"body": str(args["comment"])}
            )
        try:
            closed = await self.client.send(
                "PATCH", f"issues/{number}", {"state": "closed", "state_reason": reason}
            )
        except GitHubError:
            if comment:  # don't leave half an action behind
                await self.client.request("DELETE", f"issues/comments/{comment['id']}")
            raise
        before.state.update({"closed": closed, "comment": comment})
        summary = f"Closed issue #{number} ({reason.replace('_', ' ')})"
        if comment:
            summary += " with a comment"
        return ToolResult(
            structured_content={"number": number, "state": closed["state"]}, summary=summary
        )

    async def _comment(self, args: dict[str, Any], before: Capture) -> ToolResult:
        number = int(args["number"])
        body = str(args["body"])
        if not body.strip():
            raise ToolError("body is empty")
        comment = await self.client.send("POST", f"issues/{number}/comments", {"body": body})
        before.state["comment"] = comment
        return ToolResult(
            structured_content={"comment_id": comment["id"], "url": comment.get("html_url")},
            summary=f"Commented on issue #{number}",
        )

    async def _update_file(self, args: dict[str, Any], before: Capture) -> ToolResult:
        path, branch, sha = before.state["path"], before.state["branch"], before.state["sha"]
        message = str(args.get("message") or "").strip()
        if not message:
            raise ToolError("message is empty")
        content = str(args["content"])
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        try:
            out = await self.client.send("PUT", content_path(path), body)
        except GitHubError as exc:
            if exc.status == 409:
                raise ToolError(f"{path} changed while it was being updated; retry") from exc
            raise
        blob_sha, commit_sha = out["content"]["sha"], out["commit"]["sha"]
        before.state.update(
            {"agent_sha": blob_sha, "commit_sha": commit_sha, "agent_text": content[:TEXT_LIMIT]}
        )
        verb = "Updated" if sha else "Created"
        return ToolResult(
            structured_content={"path": path, "commit_sha": commit_sha, "blob_sha": blob_sha},
            summary=f"{verb} {path} on {branch}",
        )

    async def capture_after(self, call: ToolCall, before: Capture, result: ToolResult) -> Capture:
        # Hashes come from the write responses: the values the agent left.
        state = before.state
        if call.tool == "github_close_issue":
            closed, comment = state["closed"], state.get("comment")
            hashes = {f"github:issues/{closed['number']}/state": field_hash(state_value(closed))}
            if comment:
                hashes[f"github:comments/{comment['id']}"] = field_hash(comment.get("body") or "")
            return Capture(ref=f"github:issues/{closed['number']}", after_hashes=hashes)
        if call.tool == "github_comment":
            comment = state["comment"]
            return Capture(
                ref=comment.get("html_url"),
                after_hashes={
                    f"github:comments/{comment['id']}": field_hash(comment.get("body") or "")
                },
            )
        if call.tool == "github_update_file":
            key = self.file_key(state["path"], state["branch"])
            return Capture(
                ref=f"github:commit/{state['commit_sha']}",
                after_hashes={key: field_hash(state["agent_sha"])},
            )
        return Capture()

    async def build_inverse(
        self, call: ToolCall, before: Capture, after: Capture, result: ToolResult
    ) -> Inverse | None:
        state = before.state
        if call.tool == "github_close_issue":
            issue, comment = state["issue"], state.get("comment")
            return Inverse(
                kind="github.reopen_issue",
                payload={
                    "number": issue["number"],
                    "title": issue.get("title") or "",
                    "before": state_value(issue),
                    "after": state_value(state["closed"]),
                    "comment": {"id": comment["id"], "body": (comment.get("body") or "")[:2000]}
                    if comment
                    else None,
                },
            )
        if call.tool == "github_comment":
            comment = state["comment"]
            return Inverse(
                kind="github.delete_comment",
                payload={
                    "number": int(call.args["number"]),
                    "comment": {"id": comment["id"], "body": (comment.get("body") or "")[:2000]},
                },
            )
        if call.tool == "github_update_file":
            return Inverse(
                kind="github.restore_file",
                payload={
                    "path": state["path"],
                    "branch": state["branch"],
                    "before_sha": state["sha"],
                    "before_text": state["text"],
                    "agent_sha": state["agent_sha"],
                    "agent_text": state["agent_text"],
                    "commit_sha": state["commit_sha"],
                },
            )
        return None

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
            detail=detail,
        )

    async def _revert(self, action: Action, *, apply: bool) -> RewindStep:
        inverse = action.inverse
        if inverse is None:
            return self._step(action, [], StepResult.skipped, "Nothing to undo.")
        try:
            if inverse.kind == "github.reopen_issue":
                changes, todo = await self._plan_issue(action, inverse.payload)
            elif inverse.kind == "github.delete_comment":
                changes, todo = await self._plan_comment(action, inverse.payload["comment"])
            elif inverse.kind == "github.restore_file":
                changes, todo = await self._plan_file(action, inverse.payload)
            else:
                return self._step(action, [], StepResult.failed, f"Unknown inverse {inverse.kind}")
            kept = any(c.outcome is FieldChangeOutcome.keep_human_edit for c in changes)
            steps = [label for label, _ in todo]
            detail = "; ".join(steps) if steps else "Nothing to restore."
            if kept:
                detail += " (kept a human edit)"
            if not apply:
                return self._step(action, changes, StepResult.pending, detail)
            for _, run in todo:
                await run()
        except (GitHubError, ToolError) as exc:
            return self._step(action, [], StepResult.failed, str(exc))
        result = StepResult.kept_human_edit if kept else StepResult.restored
        return self._step(action, changes, result, detail)

    async def _plan_issue(
        self, action: Action, p: dict[str, Any]
    ) -> tuple[list[FieldChange], list[tuple[str, Any]]]:
        number = int(p["number"])
        key = f"github:issues/{number}/state"
        issue = await self.client.get_or_none(f"issues/{number}")
        if issue is None:
            raise ToolError(f"Issue #{number} no longer exists")
        current = state_value(issue)
        # A human who already put it back hasn't changed anything worth keeping.
        ok = field_hash(current) == action.after_hashes.get(key) or current == p["before"]
        changes = [
            FieldChange(
                field=key,
                before=p["before"],
                agent_value=p.get("after"),
                current=current,
                outcome=_outcome(ok),
            )
        ]
        todo: list[tuple[str, Any]] = []
        if ok and current != p["before"]:
            verb = "Reopen" if p["before"]["state"] == "open" else "Restore the state of"
            todo.append(
                (f"{verb} #{number}", lambda: self._set_state(number, current, p["before"]))
            )
        if p.get("comment"):
            more, comment_todo = await self._plan_comment(action, p["comment"])
            changes += more
            todo += comment_todo
        return changes, todo

    async def _set_state(
        self, number: int, current: dict[str, Any], target: dict[str, Any]
    ) -> None:
        if target["state"] == "open":
            # Put back the reason an open issue has (none); GitHub may still record "reopened",
            # which state_value ignores.
            await self.client.send(
                "PATCH", f"issues/{number}", {"state": "open", "state_reason": None}
            )
            return
        if current["state"] == "closed":
            # state_reason is ignored unless the state changes, so go through open.
            await self.client.send("PATCH", f"issues/{number}", {"state": "open"})
        await self.client.send(
            "PATCH",
            f"issues/{number}",
            {"state": "closed", "state_reason": target["state_reason"] or "completed"},
        )

    async def _plan_comment(
        self, action: Action, comment: dict[str, Any]
    ) -> tuple[list[FieldChange], list[tuple[str, Any]]]:
        cid = int(comment["id"])
        key = f"github:comments/{cid}"
        live = await self.client.get_or_none(f"issues/comments/{cid}")
        current = (live.get("body") or "") if live else None
        ok = live is None or field_hash(current) == action.after_hashes.get(key)
        change = FieldChange(
            field=key,
            before=None,
            agent_value=comment.get("body"),
            current=current,
            outcome=_outcome(ok),
        )
        todo: list[tuple[str, Any]] = []
        if ok and live is not None:
            todo.append(
                (
                    f"Delete comment {cid}",
                    lambda: self.client.request("DELETE", f"issues/comments/{cid}", allow_404=True),
                )
            )
        return [change], todo

    async def _plan_file(
        self, action: Action, p: dict[str, Any]
    ) -> tuple[list[FieldChange], list[tuple[str, Any]]]:
        path, branch, before_sha = p["path"], p["branch"], p.get("before_sha")
        key = self.file_key(path, branch)
        item = await self._file(path, branch)
        current_sha = item["sha"] if item else None
        ok = (
            field_hash(current_sha if item else MISSING) == action.after_hashes.get(key)
            or current_sha == before_sha
        )
        change = FieldChange(
            field=key,
            before={"blob_sha": before_sha, "text": p.get("before_text")} if before_sha else None,
            agent_value={"blob_sha": p.get("agent_sha"), "text": p.get("agent_text")},
            current={"blob_sha": current_sha, "text": _text(item)} if item else None,
            outcome=_outcome(ok),
        )
        todo: list[tuple[str, Any]] = []
        if ok and current_sha != before_sha:
            if before_sha is None:
                todo.append(
                    (f"Delete {path} (the agent created it)", lambda: self._delete_file(p, item))
                )
            else:
                todo.append((f"Restore {path}", lambda: self._restore_file(p, current_sha)))
        return [change], todo

    async def _restore_file(self, p: dict[str, Any], current_sha: str | None) -> None:
        blob = await self.client.get(f"git/blobs/{p['before_sha']}")
        body: dict[str, Any] = {
            "message": f"Mulligan rewind: restore {p['path']}",
            "content": str(blob["content"]).replace("\n", ""),
            "branch": p["branch"],
        }
        if current_sha:
            body["sha"] = current_sha
        await self._write_file("PUT", p["path"], body)

    async def _delete_file(self, p: dict[str, Any], item: dict[str, Any] | None) -> None:
        if not item:
            return
        body = {
            "message": f"Mulligan rewind: delete {p['path']} (created by an agent)",
            "sha": item["sha"],
            "branch": p["branch"],
        }
        await self._write_file("DELETE", p["path"], body)

    async def _write_file(self, method: str, path: str, body: dict[str, Any]) -> None:
        try:
            await self.client.send(method, content_path(path), body)
        except GitHubError as exc:
            if exc.status == 409:
                raise ToolError(f"{path} changed during the rewind; nothing was written") from exc
            raise

    # --- checksums ----------------------------------------------------------------------------

    async def checksum(self) -> str | None:
        """Sum (mod 2^256) of one hash per issue (``issue_view``), per issue comment and per file
        on the default branch (path + blob sha): a sum so each element can be swapped alone."""
        issues, comments, tree = await asyncio.gather(
            self.client.paginate("issues", {"state": "all"}),
            self.client.paginate("issues/comments"),
            self.client.get(f"git/trees/{self.default_branch}", {"recursive": "1"}),
        )
        total = sum(_term("issue", issue_view(i)) for i in issues if not i.get("pull_request"))
        # Comments by issue and body, not id: a reset that re-adds a seed comment gives it a new id.
        total += sum(_term("comment", comment_issue(c), c.get("body") or "") for c in comments)
        total += sum(
            _term("file", f["path"], f["sha"]) for f in tree.get("tree", []) if f["type"] == "blob"
        )
        return f"{total % MOD:064x}"

    async def verify_scope(
        self, actions: list[Action], steps: list[RewindStep]
    ) -> tuple[str, str] | None:
        """(expected, actual) over exactly the fields the rewound actions touched: each issue's
        state, each comment the agent added and each file it wrote. Expected is the value before
        the oldest action touched the field (absent for comments and created files), or the live
        value where a step kept a human edit. Edits anywhere else don't count."""
        kept = {
            c.field
            for s in steps
            for c in s.changes
            if c.outcome is FieldChangeOutcome.keep_human_edit
        }
        expected: dict[str, Any] = {}
        files: dict[str, tuple[str, str]] = {}
        for action in sorted(actions, key=lambda a: a.created_at):
            inv = action.inverse
            if inv is None:
                continue
            p = inv.payload
            if inv.kind == "github.reopen_issue":
                expected.setdefault(f"github:issues/{p['number']}/state", p["before"])
            if inv.kind in ("github.reopen_issue", "github.delete_comment") and p.get("comment"):
                expected.setdefault(f"github:comments/{p['comment']['id']}", MISSING)
            if inv.kind == "github.restore_file":
                key = self.file_key(p["path"], p["branch"])
                expected.setdefault(key, p["before_sha"] or MISSING)
                files[key] = (p["path"], p["branch"])
        if not expected:
            return None
        current = await self._current(expected, files)
        exp = act = 0
        for key, value in expected.items():
            now = current[key]
            exp += _term(key, field_hash(now if key in kept else value))
            act += _term(key, field_hash(now))
        return f"{exp % MOD:064x}", f"{act % MOD:064x}"

    async def _current(
        self, keys: dict[str, Any], files: dict[str, tuple[str, str]]
    ) -> dict[str, Any]:
        """The live value of each field path."""
        out: dict[str, Any] = {}
        numbers = [k.split("/")[1] for k in keys if k.startswith("github:issues/")]
        cids = [k.split("/")[1] for k in keys if k.startswith("github:comments/")]
        if numbers:
            issues = await self.client.paginate("issues", {"state": "all"})
            by_number = {str(i["number"]): i for i in issues}
            for n in numbers:
                issue = by_number.get(n)
                out[f"github:issues/{n}/state"] = state_value(issue) if issue else MISSING
        if cids:
            comments = {str(c["id"]): c for c in await self.client.paginate("issues/comments")}
            for cid in cids:
                c = comments.get(cid)
                out[f"github:comments/{cid}"] = (c.get("body") or "") if c else MISSING
        for key, (path, branch) in files.items():
            item = await self._file(path, branch)
            out[key] = item["sha"] if item else MISSING
        return out


def _outcome(ok: bool) -> FieldChangeOutcome:
    return FieldChangeOutcome.restore if ok else FieldChangeOutcome.keep_human_edit


def _clean_path(path: str) -> str:
    clean = path.strip().strip("/")
    parts = clean.split("/")
    if not clean or any(p in ("", ".", "..") for p in parts) or "\\" in clean:
        raise ToolError(f"bad path {path!r}")
    if parts[0] == ".git" or clean.startswith(".github/workflows"):
        raise ToolError(f"{clean} can't be written through Mulligan")
    return clean


__all__ = ["GitHubAdapter", "ToolError", "canon", "field_hash", "issue_view", "state_value"]
