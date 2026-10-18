// HTTP surface of the Photon service (contracts/photon.openapi.yaml), called by the gateway.
// Phase 1 skeleton: health and auth are real; the /notify/* routes answer 501 until Phase 2.

import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import type { ErrorResponse } from "./types.ts";

export type AppOptions = { serviceToken: string };

function send(res: ServerResponse, status: number, body: unknown): void {
  res.writeHead(status, { "content-type": "application/json" });
  res.end(JSON.stringify(body));
}

function error(res: ServerResponse, status: number, detail: string): void {
  send(res, status, { detail } satisfies ErrorResponse);
}

export function tokenMatches(given: string | string[] | undefined, expected: string): boolean {
  if (typeof given !== "string") return false;
  const a = Buffer.from(given);
  const b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}

export function createApp({ serviceToken }: AppOptions): Server {
  return createServer((req: IncomingMessage, res: ServerResponse) => {
    const path = new URL(req.url ?? "/", "http://localhost").pathname;

    if (req.method === "GET" && path === "/healthz") return send(res, 200, { ok: true });

    if (!tokenMatches(req.headers["x-mulligan-service-token"], serviceToken)) {
      return error(res, 401, "missing or invalid token");
    }

    if (req.method === "POST" && (path === "/notify/approval" || path === "/notify/summary")) {
      req.resume(); // drain the body; Phase 2 parses and validates it
      return error(res, 501, "not implemented yet");
    }

    return error(res, 404, "not found");
  });
}
