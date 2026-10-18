# 01-mcp-http — spikes 0.1 and 0.2 (Stream A)

One process, two uvicorn servers: MCP (`/mcp`, Streamable HTTP, per-agent bearer token) on `MULLIGAN_MCP_PORT` and REST (`/healthz`, `/api/calls`, API token) on `MULLIGAN_API_PORT`, both bound to 127.0.0.1. Results and gotchas are in `docs/SDK_NOTES.md`.

| File | What it does |
|---|---|
| `server.py` | The two servers, the `whoami` tool, token checks and the allowed-hosts setting |
| `client_check.py <mcp-url>` | Lists and calls `whoami`, checks the 401s and that nothing but `/mcp` answers on that origin |
| `devin_up.sh` | Starts a quick tunnel and the server, runs `client_check.py` through the tunnel, prints the Devin settings |

`.env` (gitignored) holds throwaway tokens and the ports (18000/18001). `devin_up.sh` creates it if it's missing.

## Run locally

```bash
uv run --env-file .env python server.py
uv run --env-file .env python client_check.py http://127.0.0.1:18001/mcp
```

## Devin

1. Run `./devin_up.sh` and leave it running. It prints the tunnel URL and copies `Bearer <SPIKE_AGENT_TOKEN>` to the clipboard.
2. In Devin (needs the Manage MCP Servers permission): Customize → MCPs → Add MCP → Add custom MCP.
3. Server Name `Mulligan spike`, Transport `HTTP`, Server URL `https://<tunnel>.trycloudflare.com/mcp` (no trailing slash).
4. Auth: Auth Header, key `Authorization`, value from the clipboard.
5. Save, then Test tools: it should list `whoami`.
6. Start a Devin session and ask it to call the `whoami` tool on the Mulligan spike MCP server. The terminal running `devin_up.sh` shows the `tools/call whoami` line with Devin's session id and protocol version.

Restarting the script gives a new tunnel URL, so update the Server URL in Devin after a restart.
