"""Acme Widgets on GitHub: 45 issues (40 stale, 5 active), a few labels, a few comments and
docs/pricing.md, in GITHUB_ORG/GITHUB_REPO.

Idempotent and deterministic. Every seed issue and comment carries a hidden marker, so a re-run
finds them instead of creating duplicates. Issues can't be deleted, so a reset reopens closed seed
issues, puts back their title, body, labels and assignees, deletes comments on them that aren't
part of the seed and restores seed comments and files. The gateway's demo reset calls ``reset``.

  uv --directory gateway run python ../demo/seed/github_seed.py          # seed or reset
  uv --directory gateway run python ../demo/seed/github_seed.py --check  # list drift only
"""

from __future__ import annotations

import asyncio
import base64
import random
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from mulligan.adapters.github.client import GitHubClient, content_path

MARKER = "<!-- mulligan-seed:{kind}:{key} -->"
MARKER_RE = re.compile(r"<!-- mulligan-seed:(issue|comment):(\d+) -->")

LABELS = {
    "stale": ("cfd3d7", "No activity in 90+ days"),
    "pricing": ("fbca04", "Prices, plans and billing"),
    "checkout": ("1d76db", "Cart and checkout"),
    "inventory": ("0e8a16", "Stock levels and warehouse sync"),
    "customer-report": ("d93f0b", "Reported by a customer"),
}

COMPONENTS = [
    ("checkout page", "checkout"),
    ("cart", "checkout"),
    ("invoice PDF", "pricing"),
    ("inventory sync", "inventory"),
    ("widget configurator", None),
    ("order history", None),
    ("CSV export", None),
    ("product search", None),
    ("shipping estimator", "checkout"),
    ("admin dashboard", None),
    ("email receipts", "checkout"),
    ("coupon codes", "pricing"),
    ("product images", None),
    ("warehouse API", "inventory"),
    ("tax calculation", "pricing"),
    ("returns form", None),
    ("login page", None),
    ("mobile navigation", None),
    ("price list", "pricing"),
    ("stock alerts", "inventory"),
]
# Bug titles: "<Component> <problem>", with the verb agreeing ({s} -> "s" for a singular subject).
PROBLEMS = [
    "{is} slow with more than 500 items",
    "show{s} the wrong currency for EU customers",
    "break{s} on Safari 15",
    "time{s} out during the holiday peak",
    "drop{s} the last line item",
    "ignore{s} the customer's timezone",
    "render{s} blank in dark mode",
    "double-count{s} discounts",
]
IDEAS = [
    "Add bulk edit to the {c}",
    "Support SSO on the {c}",
    "Make the {c} keyboard accessible",
    "Add an audit trail to the {c}",
    "Translate the {c} into Spanish",
    "Cache responses from the {c}",
]
PLURAL = {"email receipts", "coupon codes", "product images", "stock alerts"}
MONTHS = ["January", "February", "March", "April", "May", "June"]

# (seed position, title, body, labels): the issues a cleanup must leave open.
ACTIVE = {
    5: (
        "Pricing page still shows the old Widget Pro price",
        "The public pricing page lists Widget Pro at $89, but the price list says $99.",
        ["pricing", "bug"],
    ),
    14: (
        "Inventory sync double-counts returns",
        "Returned items are added back to stock twice when the warehouse API retries.",
        ["inventory", "bug"],
    ),
    23: (
        "Checkout fails for orders over $10,000",
        "Large B2B orders hit a payment timeout at the last step. Two customers this week.",
        ["checkout", "bug", "customer-report"],
    ),
    32: (
        "Add an annual billing option",
        "Several Enterprise Kit customers asked to pay yearly with a discount.",
        ["pricing", "enhancement"],
    ),
    41: (
        "Widget configurator crashes on iPad",
        "Rotating the iPad while the 3D preview loads crashes the tab.",
        ["bug", "customer-report"],
    ),
}

COMMENTS = {
    1: (2, "Still happening on 2.3, but only for carts with gift cards."),
    2: (5, "Marketing needs this fixed before the price change email goes out."),
    3: (9, "I couldn't reproduce this on the latest build. Leaving it open for now."),
    4: (14, "Warehouse team confirmed: 18 SKUs are off by the number of returns."),
    5: (23, "Workaround for now: split the order into two invoices."),
}

PRICING_PATH = "docs/pricing.md"
PRICING = """# Acme Widgets pricing

Prices are in USD and exclude tax. The price list in the shop database is the source of truth;
this page is what customers see.

| SKU | Product | Price |
|---|---|---|
| WID-001 | Classic Widget | $49.00 |
| WID-002 | Widget Pro | $99.00 |
| WID-003 | Widget Mini | $19.00 |
| GAD-001 | Gadget | $149.00 |
| GAD-002 | Gadget XL | $249.00 |
| SPR-001 | Sprocket (10 pack) | $12.50 |
| SPR-002 | Sprocket (100 pack) | $99.50 |
| KIT-001 | Starter Kit | $199.00 |
| KIT-002 | Enterprise Kit | $1,299.00 |

## Discounts

- Orders over $5,000: 5% off.
- Legacy customers keep their contract prices until renewal (see the legacy price list).

## Questions

Email sales@acme-widgets.test.
"""


@dataclass(frozen=True)
class SeedIssue:
    key: int
    title: str
    body: str
    labels: tuple[str, ...]
    stale: bool


@dataclass
class Report:
    """What a run found and did. ``numbers`` maps seed key -> issue number."""

    numbers: dict[int, int] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)


def seed_issues() -> list[SeedIssue]:
    rng = random.Random(20261018)  # noqa: S311 (fixed demo data, not crypto)
    picks = [rng.sample(range(len(PROBLEMS) if i % 2 == 0 else len(IDEAS)), 2) for i in range(20)]
    issues: list[SeedIssue] = []
    stale = 0
    for key in range(1, 46):
        if key in ACTIVE:
            title, text, labels = ACTIVE[key]
            body = f"{text}\n\n{MARKER.format(kind='issue', key=key)}"
            issues.append(SeedIssue(key, title, body, tuple(sorted(labels)), stale=False))
            continue
        index, round_ = stale % len(COMPONENTS), stale // len(COMPONENTS)
        component, area = COMPONENTS[index]
        # Each component is either always a bug or always an idea (20 is even), and gets two
        # different problems or ideas, so no two titles repeat.
        if index % 2 == 0:
            kind = "bug"
            words = {"is": "are", "s": ""} if component in PLURAL else {"is": "is", "s": "s"}
            problem = PROBLEMS[picks[index][round_] % len(PROBLEMS)].format(**words)
            title = f"{component[0].upper()}{component[1:]} {problem}"
            text = "Seen by a few customers. Steps to reproduce are in the support ticket."
        else:
            kind = "enhancement"
            title = IDEAS[picks[index][round_] % len(IDEAS)].format(c=component)
            text = "Came up in a customer call. Nobody has picked it up yet."
        month = MONTHS[rng.randrange(len(MONTHS))]
        day = rng.randrange(1, 28)
        body = (
            f"{text}\n\nOpened in {month} 2025. No activity since {month} {day}, 2025.\n\n"
            f"{MARKER.format(kind='issue', key=key)}"
        )
        labels = sorted({"stale", kind, *([area] if area else [])})
        issues.append(SeedIssue(key, title, body, tuple(labels), stale=True))
        stale += 1
    assert stale == 40 and len(issues) == 45
    return issues


def _marker(body: str | None, kind: str) -> int | None:
    match = MARKER_RE.search(body or "")
    return int(match.group(2)) if match and match.group(1) == kind else None


async def apply(client: GitHubClient, *, write: bool = True) -> Report:
    """Bring the repository to the seed. With ``write=False``, only report what differs."""
    report = Report()

    async def do(description: str, method: str, path: str, body: Any = None) -> Any:
        report.actions.append(description)
        return await client.send(method, path, body) if write else None

    # Labels
    existing = {lb["name"]: lb for lb in await client.paginate("labels")}
    for name, (color, description) in LABELS.items():
        label = existing.get(name)
        if label is None:
            await do(
                f"create label {name}",
                "POST",
                "labels",
                {"name": name, "color": color, "description": description},
            )
        elif (label.get("color"), label.get("description") or "") != (color, description):
            await do(
                f"fix label {name}",
                "PATCH",
                f"labels/{name}",
                {"color": color, "description": description},
            )

    # Issues
    found: dict[int, dict[str, Any]] = {}
    for issue in await client.paginate("issues", {"state": "all", "direction": "asc"}):
        key = _marker(issue.get("body"), "issue")
        if key is not None and not issue.get("pull_request") and key not in found:
            found[key] = issue
    for seed in seed_issues():
        issue = found.get(seed.key)
        if issue is None:
            created = await do(
                f"create issue {seed.key}",
                "POST",
                "issues",
                {"title": seed.title, "body": seed.body, "labels": list(seed.labels)},
            )
            if created:
                report.numbers[seed.key] = created["number"]
            continue
        number = issue["number"]
        report.numbers[seed.key] = number
        patch: dict[str, Any] = {}
        if issue.get("title") != seed.title:
            patch["title"] = seed.title
        if (issue.get("body") or "") != seed.body:
            patch["body"] = seed.body
        if sorted(lb["name"] for lb in issue.get("labels") or []) != list(seed.labels):
            patch["labels"] = list(seed.labels)
        if issue.get("assignees"):
            patch["assignees"] = []
        if issue.get("state") != "open":
            patch["state"] = "open"
        if patch:
            await do(
                f"restore #{number} ({', '.join(sorted(patch))})",
                "PATCH",
                f"issues/{number}",
                patch,
            )

    # Comments: seed ones put back, anything else on a seed issue deleted.
    by_number = {n: k for k, n in report.numbers.items()}
    seen: set[int] = set()
    for comment in await client.paginate("issues/comments"):
        number = int(str(comment.get("issue_url", "")).rsplit("/", 1)[-1] or 0)
        if number not in by_number:
            continue
        key = _marker(comment.get("body"), "comment")
        if key is None or key in seen or COMMENTS.get(key, (0,))[0] != by_number[number]:
            await do(
                f"delete comment {comment['id']} on #{number}",
                "DELETE",
                f"issues/comments/{comment['id']}",
            )
            continue
        seen.add(key)
        want = _comment_body(key)
        if comment.get("body") != want:
            await do(
                f"restore comment {comment['id']}",
                "PATCH",
                f"issues/comments/{comment['id']}",
                {"body": want},
            )
    for key, (issue_key, _) in COMMENTS.items():
        if key not in seen and issue_key in report.numbers:
            number = report.numbers[issue_key]
            await do(
                f"add comment {key} on #{number}",
                "POST",
                f"issues/{number}/comments",
                {"body": _comment_body(key)},
            )

    # docs/pricing.md
    item = await client.get_or_none(content_path(PRICING_PATH))
    current = base64.b64decode(item["content"]).decode() if item and item.get("content") else None
    if current != PRICING:
        body: dict[str, Any] = {
            "message": f"Seed {PRICING_PATH}",
            "content": base64.b64encode(PRICING.encode()).decode(),
        }
        if item:
            body["sha"] = item["sha"]
        await do(f"write {PRICING_PATH}", "PUT", content_path(PRICING_PATH), body)
    return report


def _comment_body(key: int) -> str:
    return f"{COMMENTS[key][1]}\n\n{MARKER.format(kind='comment', key=key)}"


async def reset(client: GitHubClient) -> Report:
    """The gateway's demo reset hook."""
    return await apply(client)


def stale_numbers(report: Report) -> list[int]:
    return sorted(
        report.numbers[s.key] for s in seed_issues() if s.stale and s.key in report.numbers
    )


async def main(argv: list[str]) -> int:
    import os
    from pathlib import Path

    from mulligan.settings import REPO_ROOT, load_dotenv

    # MULLIGAN_DOTENV points at another checkout's .env (e.g. from a git worktree).
    dotenv = Path(os.environ.get("MULLIGAN_DOTENV") or REPO_ROOT / ".env")
    env = {**load_dotenv(dotenv), **os.environ}
    token, org, repo = (env.get(k, "") for k in ("GITHUB_TOKEN", "GITHUB_ORG", "GITHUB_REPO"))
    if not (token and org and repo):
        print("GITHUB_TOKEN, GITHUB_ORG and GITHUB_REPO must be set (.env)", file=sys.stderr)
        return 2
    check = "--check" in argv
    client = GitHubClient(token, org, repo)
    try:
        report = await apply(client, write=not check)
        for line in report.actions:
            print(("would " if check else "") + line)
        if not check and report.actions:
            report = await apply(client, write=False)  # re-read, and pick up created numbers
            if report.actions:
                print("still differs: " + "; ".join(report.actions), file=sys.stderr)
                return 1
    finally:
        await client.aclose()
    active = sorted(report.numbers[k] for k in ACTIVE if k in report.numbers)
    print(f"{org}/{repo}: {len(report.numbers)} seed issues")
    print("stale: " + ",".join(map(str, stale_numbers(report))))
    print("active: " + ",".join(map(str, active)))
    print(f"drift: {len(report.actions)}" if check else "clean")
    return 1 if check and report.actions else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
