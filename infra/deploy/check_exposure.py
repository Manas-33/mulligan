"""Check what the gateway exposes, locally and through the tunnel.

  uv --directory gateway run python ../infra/deploy/check_exposure.py

Locally: every REST route in contracts/openapi.yaml refuses a missing token (only /healthz is open),
the MCP endpoint refuses a missing or wrong token, the MCP port serves nothing but /mcp, and
neither port answers on the laptop's LAN address (both bind to 127.0.0.1).
Through the tunnel (when MULLIGAN_MCP_PUBLIC_URL is set): /api/* and /healthz aren't reachable,
/mcp refuses a missing token, and the first agent token can initialize.
Exits non-zero on any failure.
"""

from __future__ import annotations

import os
import socket
import sys

import httpx
import yaml

from mulligan.settings import REPO_ROOT, load_dotenv

for _key, _value in load_dotenv(REPO_ROOT / ".env").items():
    os.environ.setdefault(_key, _value)

API_PORT = int(os.environ.get("MULLIGAN_API_PORT", "8000"))
MCP_PORT = int(os.environ.get("MULLIGAN_MCP_PORT", "8001"))
API = f"http://127.0.0.1:{API_PORT}"
MCP = f"http://127.0.0.1:{MCP_PORT}/mcp"
PUBLIC = os.environ.get("MULLIGAN_MCP_PUBLIC_URL", "").rstrip("/")
DASH = {"Authorization": f"Bearer {os.environ.get('MULLIGAN_DASHBOARD_TOKEN', '')}"}
AGENT_TOKEN = os.environ.get("MULLIGAN_AGENT_TOKENS", ":").split(",")[0].partition(":")[2].strip()
SAMPLE_IDS = {
    "{session_id}": "ses_" + "0" * 26,
    "{action_id}": "act_" + "0" * 26,
    "{rewind_job_id}": "rwd_" + "0" * 26,
}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "exposure-check", "version": "0"},
    },
}
MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


def routes() -> list[tuple[str, str]]:
    spec = yaml.safe_load((REPO_ROOT / "contracts/openapi.yaml").read_text())
    out = []
    for path, ops in spec["paths"].items():
        for method in ops:
            if method in {"get", "post"}:
                for placeholder, value in SAMPLE_IDS.items():
                    path = path.replace(placeholder, value)
                out.append((method.upper(), path))
    return out


def lan_ip() -> str | None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            ip: str = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def refuses(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.5):
            return False
    except OSError:
        return True


def main() -> None:
    with httpx.Client(timeout=10) as http:
        for method, path in routes():
            r = http.request(method, API + path)
            want = 200 if path == "/healthz" else 401
            check(r.status_code == want, f"{method} {path} without a token -> {r.status_code}")
        check(
            http.post(MCP, json=INIT, headers=MCP_HEADERS).status_code == 401,
            "MCP without a token -> 401",
        )
        wrong = {**MCP_HEADERS, "authorization": "Bearer wrong"}
        check(
            http.post(MCP, json=INIT, headers=wrong).status_code == 401,
            "MCP with a wrong token -> 401",
        )
        origin = MCP.rsplit("/", 1)[0]
        for path in ["/api/sessions", "/healthz", "/docs", "/openapi.json"]:
            r = http.get(origin + path, headers=DASH)
            check(r.status_code == 404, f"MCP port {path} -> {r.status_code}")

        ip = lan_ip()
        if ip:
            for port in (API_PORT, MCP_PORT):
                check(refuses(ip, port), f"nothing answers on {ip}:{port} (LAN)")
        else:
            print("skip  no LAN address found")

        if not PUBLIC:
            print("skip  MULLIGAN_MCP_PUBLIC_URL is not set, so the tunnel isn't checked")
        else:
            base = PUBLIC.removesuffix("/mcp")
            for path in ["/api/sessions", "/api/checksums", "/healthz"]:
                r = http.get(base + path, headers=DASH)
                check(r.status_code == 404, f"tunnel {path} -> {r.status_code}")
            r = http.post(base + "/mcp", json=INIT, headers=MCP_HEADERS)
            check(r.status_code == 401, f"tunnel MCP without a token -> {r.status_code}")
            if AGENT_TOKEN:
                agent = {**MCP_HEADERS, "authorization": f"Bearer {AGENT_TOKEN}"}
                r = http.post(base + "/mcp", json=INIT, headers=agent)
                check(r.status_code == 200, f"tunnel MCP with an agent token -> {r.status_code}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
