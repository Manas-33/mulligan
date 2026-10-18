// HTTP surface of the Photon service (contracts/photon.openapi.yaml), called by the gateway.

import { timingSafeEqual } from "node:crypto";
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { notifyApproval, notifySummary, type Reply } from "./notify.ts";
import type { Messenger } from "./photon.ts";
import type { ErrorResponse } from "./types.ts";

export type AppOptions = { serviceToken: string; messenger: Messenger; log?: (line: string) => void };

const MAX_BODY = 16 * 1024;

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

class BodyError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
  }
}

async function readJson(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req as AsyncIterable<Buffer>) {
    size += chunk.length;
    if (size > MAX_BODY) throw new BodyError(413, "body too large");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new BodyError(422, "body must be JSON");
  }
}

const ROUTES: Record<string, (m: Messenger, body: unknown) => Promise<Reply>> = {
  "/notify/approval": notifyApproval,
  "/notify/summary": notifySummary,
};

export function createApp({ serviceToken, messenger, log = console.error }: AppOptions): Server {
  return createServer(async (req: IncomingMessage, res: ServerResponse) => {
    const path = new URL(req.url ?? "/", "http://localhost").pathname;

    if (req.method === "GET" && path === "/healthz") return send(res, 200, { ok: true });

    if (!tokenMatches(req.headers["x-mulligan-service-token"], serviceToken)) {
      req.resume();
      return error(res, 401, "missing or invalid token");
    }

    const route = req.method === "POST" ? ROUTES[path] : undefined;
    if (!route) {
      req.resume();
      return error(res, 404, "not found");
    }

    try {
      const reply = await route(messenger, await readJson(req));
      return send(res, reply.status, reply.body);
    } catch (e) {
      if (e instanceof BodyError) return error(res, e.status, e.message);
      log(`${path} failed: ${e instanceof Error ? e.message : String(e)}`);
      return error(res, 502, "Photon request failed");
    }
  });
}
