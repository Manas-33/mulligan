"""Input guards that need no database: the MCP port's host check and the typed tools' filters."""

from __future__ import annotations

import pytest

from mulligan.adapters.postgres.catalog import Column, Table, ToolError
from mulligan.adapters.postgres.tools import build_where
from mulligan.mcp_server.server import allowed_host

PRODUCTS = Table(
    oid=1,
    schema="public",
    name="products",
    pk=("id",),
    columns={
        "id": Column(name="id", type="integer", generated=False, identity="a"),
        "name": Column(name="name", type="text", generated=False, identity=""),
    },
)


@pytest.mark.parametrize(
    ("host", "public", "ok"),
    [
        ("127.0.0.1:8001", "", True),
        ("[::1]:8001", "", True),
        ("abc.trycloudflare.com", "", True),
        ("mulligan.example.com", "mulligan.example.com", True),
        ("evil.example.com", "mulligan.example.com", False),
        ("", "", False),  # no Host header, no public URL configured
        ("", "mulligan.example.com", False),
    ],
)
def test_allowed_host(host: str, public: str, ok: bool) -> None:
    assert allowed_host(host, public) is ok


@pytest.mark.parametrize("op", ["like", "ilike"])
def test_like_needs_a_string_pattern(op: str) -> None:
    for value in [None, 5, ["a"]]:
        with pytest.raises(ToolError, match="needs a string pattern"):
            build_where(PRODUCTS, [{"column": "name", "op": op, "value": value}], False)
    where, params = build_where(PRODUCTS, [{"column": "name", "op": op, "value": "W%"}], False)
    assert params == ["W%"]


async def test_github_client_refuses_urls_off_github() -> None:
    from mulligan.adapters.github.client import GitHubClient, GitHubError

    client = GitHubClient("tok", "acme", "widgets")
    try:
        assert client.url("issues") == "/repos/acme/widgets/issues"
        next_page = "https://api.github.com/repositories/1/issues?page=2"
        assert client.url(next_page) == next_page
        with pytest.raises(GitHubError, match="refusing a URL"):
            client.url("https://evil.example.com/collect")
    finally:
        await client.aclose()
