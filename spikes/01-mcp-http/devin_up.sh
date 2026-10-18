#!/usr/bin/env bash
# One command for the Devin step: quick tunnel + spike server + self-check through the tunnel,
# then prints what to paste into Devin. Ctrl+C stops the server and the tunnel.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  (umask 077 && python3 -c 'import secrets; print(f"SPIKE_AGENT_TOKEN={secrets.token_urlsafe(32)}\nSPIKE_API_TOKEN={secrets.token_urlsafe(32)}\nMULLIGAN_API_PORT=18000\nMULLIGAN_MCP_PORT=18001")' > .env)
fi
set -a; . ./.env; set +a

for port in "$MULLIGAN_API_PORT" "$MULLIGAN_MCP_PORT"; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null; then
    echo "port $port is already in use:"; lsof -nP -iTCP:"$port" -sTCP:LISTEN; exit 1
  fi
done

LOGS=$(mktemp -d -t mulligan-spike)
cleanup() {
  trap - EXIT INT TERM
  pkill -TERM -P $$ 2>/dev/null || true  # tunnel, uv/server, log tail
  wait 2>/dev/null || true
  echo "stopped server and tunnel (logs: $LOGS)"
}
trap cleanup EXIT INT TERM

# Quick tunnels sometimes fail to register on venue Wi-Fi; QUIC (UDP 7844) is often blocked, so default to http2.
url=""
for attempt in 1 2 3; do
  cloudflared tunnel --no-autoupdate --protocol "${TUNNEL_PROTOCOL:-http2}" \
    --url "http://127.0.0.1:$MULLIGAN_MCP_PORT" > "$LOGS/tunnel.log" 2>&1 &
  tunnel_pid=$!
  for _ in $(seq 1 120); do
    grep -q "Registered tunnel connection" "$LOGS/tunnel.log" && break
    kill -0 "$tunnel_pid" 2>/dev/null || break
    sleep 1
  done
  if grep -q "Registered tunnel connection" "$LOGS/tunnel.log"; then
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGS/tunnel.log" | head -1)
    break
  fi
  echo "tunnel attempt $attempt failed:"; tail -3 "$LOGS/tunnel.log"
  kill "$tunnel_pid" 2>/dev/null || true
done
[ -n "$url" ] || { echo "could not open a quick tunnel"; exit 1; }

# The SDK's DNS-rebinding check rejects any Host it doesn't know (421), so pass the tunnel's hostname in.
MULLIGAN_MCP_PUBLIC_HOST="${url#https://}" uv run --env-file .env python server.py > "$LOGS/server.log" 2>&1 &
server_pid=$!
until grep -q "Uvicorn running on .*:$MULLIGAN_MCP_PORT" "$LOGS/server.log"; do
  kill -0 "$server_pid" 2>/dev/null || { echo "server failed:"; tail -20 "$LOGS/server.log"; exit 1; }
  sleep 0.5
done

echo "self-check through $url/mcp"
uv run --env-file .env python client_check.py "$url/mcp"

printf 'Bearer %s' "$SPIKE_AGENT_TOKEN" | pbcopy
cat <<EOF

Devin: Customize -> MCPs -> Add MCP -> Add custom MCP
  Server Name   Mulligan spike
  Transport     HTTP
  Server URL    $url/mcp
  Auth          Auth Header; key Authorization; value is on your clipboard ("Bearer <SPIKE_AGENT_TOKEN>")
Save, then click Test tools. Then start a session that calls the whoami tool.

Leave this running: a new quick tunnel gets a new URL. Ctrl+C stops everything.
Tool calls and rejected requests show below.
EOF
tail -n 0 -f "$LOGS/server.log" | grep --line-buffered -E "tools/call|rejecting|401|421|Traceback" &
# `wait` (unlike a foreground pipeline) returns as soon as a trapped signal arrives, so cleanup runs.
wait "$server_pid"
