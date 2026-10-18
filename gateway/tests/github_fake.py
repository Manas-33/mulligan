"""An in-memory GitHub repository behind ``httpx.MockTransport``: just enough of the REST API for
the GitHub adapter and the demo seed (issues, comments, labels, contents, blobs, trees)."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote

import httpx

from mulligan.adapters.github.client import GitHubClient

ORG, REPO = "acme", "widgets"
PREFIX = f"/repos/{ORG}/{REPO}"


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {}
        self.comments: dict[int, dict[str, Any]] = {}
        self.labels: dict[str, dict[str, Any]] = {
            n: {"name": n, "color": "ededed", "description": ""} for n in ("bug", "enhancement")
        }
        self.files: dict[str, bytes] = {"README.md": b"# widgets\n"}
        self.blobs: dict[str, bytes] = {blob_sha(v): v for v in self.files.values()}
        self.next_id = 1000
        self.writes: list[str] = []
        self.requests: list[str] = []
        # Returned, or raised, once each before handling a request.
        self.fail: list[httpx.Response | Exception] = []
        # Writes ("PATCH issues/2") applied, then answered with a dropped connection, once each.
        self.lose: list[str] = []

    def client(self, **kw: Any) -> GitHubClient:
        return GitHubClient(
            "test-token",
            ORG,
            REPO,
            transport=httpx.MockTransport(self.handle),
            write_interval=0,
            **kw,
        )

    # --- helpers for tests: a teammate editing GitHub directly -----------------------------------

    def add_issue(self, title: str, body: str = "", labels: list[str] | None = None) -> int:
        number = len(self.issues) + 1
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "state": "open",
            "state_reason": None,
            "labels": [{"name": n} for n in labels or []],
            "assignees": [],
            "comments": 0,
            "updated_at": "2026-10-17T00:00:00Z",
        }
        return number

    def put_file(self, path: str, text: str) -> None:
        data = text.encode()
        self.files[path] = data
        self.blobs[blob_sha(data)] = data

    # --- the API -------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-token"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        self.requests.append(f"{request.method} {request.url.path}")
        if self.fail:
            failure = self.fail.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        path = unquote(request.url.path)
        if not path.startswith(PREFIX):
            return _json(404, {"message": "Not Found"})
        rest = path[len(PREFIX) :].lstrip("/")
        body = json.loads(request.content) if request.content else {}
        params = dict(request.url.params)
        if request.method != "GET":
            self.writes.append(f"{request.method} {rest}")
        for pattern, method, handler in self.routes():
            match = re.fullmatch(pattern, rest)
            if match and request.method == method:
                response: httpx.Response = handler(request, body, params, *match.groups())
                if f"{request.method} {rest}" in self.lose:
                    self.lose.remove(f"{request.method} {rest}")
                    raise httpx.ReadTimeout("reply lost", request=request)
                return response
        return _json(404, {"message": "Not Found"})

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("", "GET", lambda *a: _json(200, {"default_branch": "main", "full_name": PREFIX})),
            ("labels", "GET", self.list_labels),
            ("labels", "POST", self.create_label),
            (r"labels/([^/]+)", "PATCH", self.update_label),
            ("issues", "GET", self.list_issues),
            ("issues", "POST", self.create_issue),
            (r"issues/comments", "GET", self.list_comments),
            (r"issues/comments/(\d+)", "GET", self.get_comment),
            (r"issues/comments/(\d+)", "PATCH", self.update_comment),
            (r"issues/comments/(\d+)", "DELETE", self.delete_comment),
            (r"issues/(\d+)", "GET", self.get_issue),
            (r"issues/(\d+)", "PATCH", self.update_issue),
            (r"issues/(\d+)/comments", "GET", self.list_issue_comments),
            (r"issues/(\d+)/comments", "POST", self.create_comment),
            (r"contents/(.+)", "GET", self.get_file),
            (r"contents/(.+)", "PUT", self.put_contents),
            (r"contents/(.+)", "DELETE", self.delete_contents),
            (r"git/blobs/([0-9a-f]+)", "GET", self.get_blob),
            (r"git/trees/([^/]+)", "GET", self.get_tree),
        ]

    def _page(self, request: httpx.Request, items: list[Any], params: dict[str, str]) -> Any:
        per_page = int(params.get("per_page", 30))
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * per_page : page * per_page]
        headers = {}
        if page * per_page < len(items):
            nxt = request.url.copy_merge_params({"page": str(page + 1), "per_page": per_page})
            headers["link"] = f'<{nxt}>; rel="next"'
        return _json(200, chunk, headers)

    def list_labels(self, request: Any, body: Any, params: Any) -> Any:
        return self._page(request, list(self.labels.values()), params)

    def create_label(self, request: Any, body: Any, params: Any) -> Any:
        self.labels[body["name"]] = {"description": "", **body}
        return _json(201, self.labels[body["name"]])

    def update_label(self, request: Any, body: Any, params: Any, name: str) -> Any:
        self.labels[name].update(body)
        return _json(200, self.labels[name])

    def _issue_json(self, issue: dict[str, Any]) -> dict[str, Any]:
        issue = dict(issue)
        issue["comments"] = sum(1 for c in self.comments.values() if c["_n"] == issue["number"])
        return issue

    def list_issues(self, request: Any, body: Any, params: dict[str, str]) -> Any:
        state = params.get("state", "open")
        want = [x for x in params.get("labels", "").split(",") if x]
        items = [
            self._issue_json(i)
            for i in sorted(self.issues.values(), key=lambda i: i["number"])
            if (state == "all" or i["state"] == state)
            and all(x in [lb["name"] for lb in i["labels"]] for x in want)
        ]
        if params.get("direction", "desc") == "desc":
            items.reverse()
        return self._page(request, items, params)

    def create_issue(self, request: Any, body: Any, params: Any) -> Any:
        number = self.add_issue(body["title"], body.get("body") or "", body.get("labels"))
        return _json(201, self._issue_json(self.issues[number]))

    def get_issue(self, request: Any, body: Any, params: Any, n: str) -> Any:
        issue = self.issues.get(int(n))
        return (
            _json(200, self._issue_json(issue)) if issue else _json(404, {"message": "Not Found"})
        )

    def update_issue(self, request: Any, body: dict[str, Any], params: Any, n: str) -> Any:
        issue = self.issues.get(int(n))
        if issue is None:
            return _json(404, {"message": "Not Found"})
        state = body.get("state", issue["state"])
        if state != issue["state"]:
            if state == "closed":
                issue["state_reason"] = body.get("state_reason") or "completed"
            else:
                issue["state_reason"] = "reopened"  # what GitHub records, even if null is sent
            issue["state"] = state
        for key in ("title", "body"):
            if key in body:
                issue[key] = body[key]
        if "labels" in body:
            issue["labels"] = [{"name": n} for n in body["labels"]]
        if "assignees" in body:
            issue["assignees"] = [{"login": n} for n in body["assignees"]]
        return _json(200, self._issue_json(issue))

    def _comment_json(self, c: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in c.items() if not k.startswith("_")}
        out["issue_url"] = f"https://api.github.com{PREFIX}/issues/{c['_n']}"
        out["html_url"] = f"https://github.com/{ORG}/{REPO}/issues/{c['_n']}#issuecomment-{c['id']}"
        return out

    def create_comment(self, request: Any, body: Any, params: Any, n: str) -> Any:
        if int(n) not in self.issues:
            return _json(404, {"message": "Not Found"})
        self.next_id += 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.comments[self.next_id] = {
            "id": self.next_id,
            "body": body["body"],
            "created_at": now,
            "updated_at": now,
            "_n": int(n),
        }
        return _json(201, self._comment_json(self.comments[self.next_id]))

    def list_issue_comments(self, request: Any, body: Any, params: dict[str, str], n: str) -> Any:
        since = params.get("since", "")
        items = [
            self._comment_json(c)
            for c in sorted(self.comments.values(), key=lambda c: c["id"])
            if c["_n"] == int(n) and c["updated_at"] >= since
        ]
        return self._page(request, items, params)

    def list_comments(self, request: Any, body: Any, params: Any) -> Any:
        items = [
            self._comment_json(c) for c in sorted(self.comments.values(), key=lambda c: c["id"])
        ]
        return self._page(request, items, params)

    def get_comment(self, request: Any, body: Any, params: Any, cid: str) -> Any:
        c = self.comments.get(int(cid))
        return _json(200, self._comment_json(c)) if c else _json(404, {"message": "Not Found"})

    def update_comment(self, request: Any, body: Any, params: Any, cid: str) -> Any:
        self.comments[int(cid)]["body"] = body["body"]
        return _json(200, self._comment_json(self.comments[int(cid)]))

    def delete_comment(self, request: Any, body: Any, params: Any, cid: str) -> Any:
        if self.comments.pop(int(cid), None) is None:
            return _json(404, {"message": "Not Found"})
        return httpx.Response(204)

    def _file_json(self, path: str) -> dict[str, Any]:
        data = self.files[path]
        return {
            "type": "file",
            "path": path,
            "sha": blob_sha(data),
            "size": len(data),
            "encoding": "base64",
            "content": base64.encodebytes(data).decode(),  # GitHub wraps lines too
        }

    def get_file(self, request: Any, body: Any, params: dict[str, str], path: str) -> Any:
        if params.get("ref", "main") != "main":
            return _json(404, {"message": "No commit found for the ref"})
        if path in self.files:
            return _json(200, self._file_json(path))
        if any(p.startswith(path + "/") for p in self.files):
            return _json(200, [{"type": "file", "path": p} for p in self.files])
        return _json(404, {"message": "Not Found"})

    def _commit(self, path: str, status: int, content: Any) -> Any:
        self.next_id += 1
        commit = {"sha": f"{self.next_id:040x}"}
        return _json(status, {"content": content, "commit": commit})

    def put_contents(self, request: Any, body: dict[str, Any], params: Any, path: str) -> Any:
        current = self.files.get(path)
        if current is not None and body.get("sha") != blob_sha(current):
            return _json(409, {"message": f"{path} does not match {body.get('sha')}"})
        if current is None and body.get("sha"):
            return _json(422, {"message": "sha given for a file that doesn't exist"})
        self.put_file(path, base64.b64decode(body["content"]).decode())
        return self._commit(path, 200 if current else 201, self._file_json(path))

    def delete_contents(self, request: Any, body: dict[str, Any], params: Any, path: str) -> Any:
        current = self.files.get(path)
        if current is None:
            return _json(404, {"message": "Not Found"})
        if body.get("sha") != blob_sha(current):
            return _json(409, {"message": "sha mismatch"})
        del self.files[path]
        return self._commit(path, 200, None)

    def get_blob(self, request: Any, body: Any, params: Any, sha: str) -> Any:
        data = self.blobs.get(sha)
        if data is None:
            return _json(404, {"message": "Not Found"})
        return _json(
            200, {"sha": sha, "encoding": "base64", "content": base64.encodebytes(data).decode()}
        )

    def get_tree(self, request: Any, body: Any, params: Any, ref: str) -> Any:
        tree = [
            {"path": p, "type": "blob", "sha": blob_sha(d)} for p, d in sorted(self.files.items())
        ]
        return _json(200, {"sha": "tree", "tree": tree, "truncated": False})


def _json(status: int, data: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=data, headers=headers)
