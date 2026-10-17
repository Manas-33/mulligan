# SDK notes

Check each SDK against its current docs before you use it. Record the pinned version, the docs link, the date you checked, and any gotchas. Add entries below; don't rewrite old ones.

Template:

```
## <SDK> — <pinned version>
Docs: <url>  ·  Checked: YYYY-MM-DD by <A|B>
- Gotcha / decision
```

## Docs index
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Claude Code MCP: https://code.claude.com/docs/en/mcp · hooks: https://code.claude.com/docs/en/hooks
- Devin custom MCP: https://docs.devin.ai/work-with-devin/mcp
- Fetch ASI:One agent: https://uagents.fetch.ai/docs/examples/asi-1
- Photon spectrum-ts: https://photon.codes/docs/spectrum-ts/introduction
- Sentry MCP monitoring (Python): https://docs.sentry.io/platforms/python/tracing/instrumentation/custom-instrumentation/mcp-module/

## Entries

_(none yet. Phase 0 fills these in)_

## MCP Python SDK + FastAPI + uvicorn + cloudflared (spikes 0.1, 0.2) — mcp 2.3.0 · FastAPI 0.143.3 · uvicorn 0.54.0 · cloudflared 2026.10.0
Docs: https://py.sdk.modelcontextprotocol.io/run/asgi/ · https://py.sdk.modelcontextprotocol.io/run/authorization/ · https://py.sdk.modelcontextprotocol.io/run/deploy/ · https://py.sdk.modelcontextprotocol.io/protocol-versions/ · https://code.claude.com/docs/en/mcp · https://docs.devin.ai/work-with-devin/mcp  ·  Checked: 2026-10-17 by A
- **0.1 pass.** Claude Code 2.1.301 lists and calls the tool through `claude mcp add --scope local --transport http mulligan <url> --header "Authorization: Bearer <token>"` and through `claude -p --mcp-config`. The server sees the token (`get_access_token()`) and the `Mcp-Session-Id`, with the handshake forced as below.
- **0.2 pass pending Devin.** Through a quick tunnel to `127.0.0.1:8001`, the Python client and Claude Code list and call the tool. A missing or wrong token gets 401. `/healthz`, `/api/*`, `/docs`, `/openapi.json` and `/.well-known/*` get 404 on the tunnel origin, because the tunnel only reaches the MCP port and it serves `/mcp` only. Devin's Test tools and a Devin session call are still to do (`spikes/01-mcp-http/README.md`).
- **v2 renames.** `from mcp.server import MCPServer` (was `FastMCP`), `from mcp.server.mcpserver import Context`. Serving options (`transport_security`, `stateless_http`, `streamable_http_path`) moved from the constructor to `streamable_http_app()`. The SDK uses `httpx2`, not `httpx`. Client: `from mcp import Client`; pass headers on your own `httpx2.AsyncClient` to `streamable_http_client(url, http_client=...)` (it rejects `headers=`).
- **Protocol 2026-07-28 has no sessions and no `Mcp-Session-Id`.** Against an SDK v2 server, Claude Code 2.1.301 negotiates 2026-07-28 by default, so the server saw no session id. Decision: wrap the MCP app in a small ASGI check (`ForceHandshake` in `spikes/01-mcp-http/server.py`). An authenticated request whose `MCP-Protocol-Version` isn't a handshake version gets a 400 JSON-RPC error. Clients in `auto` mode (Python SDK, Claude Code) treat that as a legacy server, run `initialize` (2025-11-25) and get a session id. Unauthenticated requests pass through, so they still get 401. A client pinned to 2026-07-28 can't connect. For Claude Code only, `MCP_PROTOCOL_NEGOTIATION=legacy` or `MCP_SDK_GENERATION=v1` gets the same result from the client side.
- **Inside a tool:** declare `ctx: Context`. Session id: `ctx.headers.get("mcp-session-id")`. Protocol: `ctx.protocol_version`. Agent: `get_access_token().client_id` (from `mcp.server.auth.middleware.auth_context`). The SDK binds a session to the token that opened it.
- **Auth:** `MCPServer(token_verifier=..., auth=AuthSettings(issuer_url=..., resource_server_url=None))`. The two arguments must be passed together. `issuer_url` is required but unused when `resource_server_url=None`. That setting also keeps `/.well-known/oauth-protected-resource` off and leaves `resource_metadata` out of the 401, so clients don't start an OAuth flow. A missing or wrong token gets `401` with `WWW-Authenticate: Bearer error="invalid_token"`, before any MCP parsing.
- **Mounting in FastAPI:** use `app.mount("/", mcp.streamable_http_app(...))` and keep the default path, so the endpoint is `/mcp`. The host app's lifespan must enter `mcp.session_manager.run()`: a mounted app's lifespan never runs, and the first request fails with "Task group is not initialized". `/mcp/` 307-redirects to `/mcp`, so configure clients with `/mcp` exactly.
- **FastAPI routes outside auth:** an app-level `dependencies=[Depends(auth)]` doesn't cover FastAPI's own `/docs`, `/redoc` and `/openapi.json` (200 without a token), and it doesn't cover mounted apps either. Turn those routes off on both apps (`docs_url=None, redoc_url=None, openapi_url=None`); MCP auth comes from the SDK.
- **Two servers, one process:** `await asyncio.gather(uvicorn.Server(api_cfg).serve(), uvicorn.Server(mcp_cfg).serve())`. Module-level state is shared: the REST route reads calls the MCP tool recorded. Each `serve()` installs its own SIGINT/SIGTERM handlers, so the second replaces the first, but uvicorn re-raises the captured signal on exit, so Ctrl+C and SIGTERM still stop both cleanly.
- **Allowed hosts (DNS-rebinding check):** by default only `127.0.0.1:<port>`, `localhost:<port>` and `[::1]:<port>` are allowed. cloudflared forwards the public `Host`, so through the tunnel a request with no token gets 401 (auth runs first) and one with a token gets 421 "Invalid Host header". Fix: `TransportSecuritySettings(allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", host, f"{host}:*"])`. Entries are exact strings (no `*.trycloudflare.com`), so the quick-tunnel host is passed in at start (`MULLIGAN_MCP_PUBLIC_HOST`), and a new tunnel URL means a server restart.
- **Bind and proxy headers:** bind both ports to `127.0.0.1` and point the tunnel at `http://127.0.0.1:8001`. `--url http://localhost:8001` also works against a 127.0.0.1-only bind (cloudflared falls back from `::1`). uvicorn trusts `X-Forwarded-*` from 127.0.0.1 by default and cloudflared connects from there, so the app sees `https` and the caller's IP, and redirects stay `https`.
- **cloudflared on venue Wi-Fi:** QUIC (UDP 7844) took about 60 s of dial timeouts before registering; `--protocol http2` registered in about 15 s. Creating a quick tunnel can also time out (`failed to request quick Tunnel`), so retry. The URL changes on every restart.
- **Tool results:** a bare `-> dict` return comes back as text content with `structured_content=None`. Return a TypedDict or Pydantic model to get structured output.
