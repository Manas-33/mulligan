// Spike 0.6: Photon 2.0 iMessage approval round trip (HTTP API via @photon-ai/api).
//
// Shared iMessage lines can't text first (SHARED_LINE_INBOUND_REQUIRED), so:
// 1. Find the approver's existing direct conversation with the line (they texted it once).
// 2. Send "Reply YES <code> to approve or NO <code> to reject" into that space.
// 3. Poll for the reply, parse it, and answer with a confirmation.
// Pass: the reply is parsed within 5 s of the phone sending it.
//
// Run from this folder:  npm run spike
// Reads PHOTON_PROJECT_ID, PHOTON_PROJECT_SECRET (a pho_sk_ project key) and APPROVER_HANDLE
// (iMessage phone or Apple ID email; falls back to APPROVER_PHONE) from the repo's .env.
// The approver must be on iMessage: an Android phone's RCS reply never reaches the API.

import { randomInt } from "node:crypto";
import { readFileSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import { ApiError, Photon } from "@photon-ai/api";
import { parseReply, sameHandle } from "./parse.ts";

const WAIT_MS = 180_000; // a human has 3 minutes to reply
const POLL_MS = 1_000;
const PARSE_BUDGET_MS = 5_000;

function loadEnv(path: string): Record<string, string> {
  const env: Record<string, string> = {};
  for (const raw of readFileSync(path, "utf8").split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;
    const i = line.indexOf("=");
    env[line.slice(0, i).trim()] = line.slice(i + 1).split("#")[0].trim();
  }
  return env;
}

const env = loadEnv(new URL("../../.env", import.meta.url).pathname);
const { PHOTON_PROJECT_ID: projectId, PHOTON_PROJECT_SECRET: apiKey } = env;
// APPROVER_HANDLE (phone or iMessage email) wins over APPROVER_PHONE.
const phone = env.APPROVER_HANDLE || env.APPROVER_PHONE;
if (!projectId || !apiKey || !phone) {
  console.error("Missing PHOTON_PROJECT_ID, PHOTON_PROJECT_SECRET or APPROVER_HANDLE/APPROVER_PHONE in .env");
  process.exit(2);
}

// The client types message items loosely; these are the fields the docs' program reads.
type InMsg = {
  id: string;
  createdAt: string;
  sentBy: { handle: string };
  receivedBy: { id: string };
  space: { id: string; kind: string };
  content: { type: string; text?: string };
};
const items = (page: { items: unknown[] }) => page.items as InMsg[];

const photon = new Photon({ headers: { Authorization: `Bearer ${apiKey}` } });
const path = { id: projectId };

function explain(e: unknown): string {
  if (e instanceof ApiError) {
    const body = e.body as { code?: string; detail?: string } | undefined;
    return `HTTP ${e.status} ${body?.code ?? ""} ${body?.detail ?? e.message} (request ${e.requestId ?? "?"})`;
  }
  return String(e);
}

// 1. The approver's latest direct iMessage conversation with the project's line.
const recent = await photon.projects.listMessages({
  path,
  query: { direction: "received", platform: "imessage", order: "desc", pageSize: 50 },
});
const anchor = items(recent).find((m) => sameHandle(m.sentBy?.handle, phone) && m.space?.kind === "direct");
if (!anchor) {
  console.error(`No conversation from ${phone} yet. Text "ping" to your Photon line, then rerun.`);
  process.exit(3);
}
const spaceId = anchor.space.id;
const senderId = anchor.receivedBy.id; // the line's user id; we send as the line
console.log(`Found conversation (last inbound ${anchor.createdAt}).`);

// 2. Send the approval request.
const code = String(randomInt(0, 10_000)).padStart(4, "0");
const actionId = `act_spike_${Date.now().toString(36)}`;
const sentAt = new Date();
try {
  const sent = await photon.projects.sendMessage({
    path,
    headers: { "idempotency-key": `mulligan-approval-${actionId}` },
    body: {
      spaceId,
      senderId,
      content: {
        type: "text",
        format: "plain",
        text: `Mulligan: an agent wants to email all 1,204 customers (spike ${actionId}).\nReply YES ${code} to approve or NO ${code} to reject.`,
      },
    },
  });
  console.log(`Sent approval request in ${Date.now() - sentAt.getTime()} ms (status ${sent.status}). Code ${code}. Waiting up to ${WAIT_MS / 1000}s...`);
} catch (e) {
  console.error(`Send failed: ${explain(e)}`);
  process.exit(1);
}

// 3. Poll for the reply.
const seen = new Set<string>();
const deadline = Date.now() + WAIT_MS;
while (Date.now() < deadline) {
  const page = await photon.projects.listMessages({
    path,
    query: { direction: "received", platform: "imessage", order: "asc", pageSize: 100, createdSince: sentAt.toISOString() },
  });
  const polledAt = Date.now();
  for (const msg of items(page)) {
    if (seen.has(msg.id)) continue;
    seen.add(msg.id);
    if (msg.space?.id !== spaceId || !sameHandle(msg.sentBy?.handle, phone)) continue;
    if (msg.content?.type !== "text") {
      console.log(`  ignored ${msg.content?.type} content`);
      continue;
    }
    const text = msg.content.text ?? "";
    const parsed = parseReply(text);
    // Plain message, not a threaded reply: shared lines refuse parentMessageId (409 SPACE_NOT_SENDABLE).
    // A failed confirmation is logged, not fatal: the decision is already recorded.
    const reply = async (t: string) => {
      try {
        await photon.projects.sendMessage({
          path,
          headers: { "idempotency-key": `mulligan-reply-${msg.id}` },
          body: { spaceId, senderId, content: { type: "text", format: "plain", text: t } },
        });
      } catch (e) {
        console.log(`  confirmation send failed: ${explain(e)}`);
      }
    };
    if (!parsed) {
      console.log(`  unparseable reply: ${JSON.stringify(text)}`);
      await reply(`Sorry, I need "YES ${code}" or "NO ${code}".`);
      continue;
    }
    if (parsed.code !== code) {
      console.log(`  wrong code ${parsed.code} (expected ${code})`);
      await reply(`That code doesn't match. Reply YES ${code} or NO ${code}.`);
      continue;
    }
    const lagMs = polledAt - new Date(msg.createdAt).getTime();
    await reply(parsed.decision === "approve" ? `Approved. Releasing the email. (${actionId})` : `Rejected. The email will not be sent. (${actionId})`);
    console.log(`  parsed ${parsed.decision.toUpperCase()} ${parsed.code}; phone-send -> parsed: ${lagMs} ms`);
    const ok = lagMs <= PARSE_BUDGET_MS;
    console.log(`\nSpike 0.6: ${ok ? "PASS" : "FAIL"} (reply parsed in ${lagMs} ms, budget ${PARSE_BUDGET_MS} ms), @photon-ai/api 0.5.0`);
    process.exit(ok ? 0 : 1);
  }
  await sleep(POLL_MS);
}
console.log(`\nSpike 0.6: FAIL (no valid reply within ${WAIT_MS / 1000}s)`);
process.exit(1);
