// Turns the approver's iMessage replies ("YES 4821" / "NO 4821") into gateway decisions.
// Stateless: the code is looked up among the gateway's held actions, so a restart loses nothing.

import { GatewayError, type Gateway } from "./gateway.ts";
import { parseReply } from "./parse.ts";
import type { InboundText, Messenger } from "./photon.ts";

export type ReplyLog = (line: string) => void;

/** Handle one inbound text. Returns what happened, for logs and tests. */
export async function handleReply(msg: InboundText, messenger: Messenger, gateway: Gateway): Promise<string> {
  const parsed = parseReply(msg.text);
  const say = (text: string) => messenger.send(msg.conversation, text, `mulligan-reply-${msg.id}`);

  if (!parsed) {
    // Only nudge when it looks like an attempt at an answer; ignore chatter.
    if (/^\s*(yes|y|no|n|approve|reject)\b/i.test(msg.text)) {
      await say('Reply "YES 1234" or "NO 1234" with the code from the request.');
      return "unparseable";
    }
    return "ignored";
  }

  const held = await gateway.heldActions();
  const action = held.find((a) => a.approval?.code === parsed.code);
  if (!action) {
    await say(`No request is waiting with code ${parsed.code}. It may have expired or been decided.`);
    return "no-match";
  }

  try {
    const after = await gateway.decide(action.id, {
      decision: parsed.decision,
      code: parsed.code,
      channel: "imessage",
      decided_by: `imessage:${msg.from}`,
    });
    const verdict =
      parsed.decision === "approve"
        ? after.status === "failed"
          ? `Approved, but it failed to run: ${after.error ?? "see the dashboard"}.`
          : "Approved. It's running now."
        : "Rejected. It won't run.";
    await say(`${verdict} (${action.tool}, ${action.id.slice(-6)})`);
    return parsed.decision;
  } catch (e) {
    if (e instanceof GatewayError && (e.status === 409 || e.status === 404)) {
      await say(`Code ${parsed.code} was already decided.`);
      return "already-decided";
    }
    throw e;
  }
}

/** Poll for replies until `signal` aborts. Each message is handled once. */
export async function replyLoop(
  messenger: Messenger,
  gateway: Gateway,
  opts: { intervalMs?: number; signal?: AbortSignal; log?: ReplyLog; now?: () => Date } = {},
): Promise<void> {
  const { intervalMs = 1000, signal, log = console.log, now = () => new Date() } = opts;
  const seen = new Set<string>();
  let since = now().toISOString(); // replies sent before startup are not ours to decide
  while (!signal?.aborted) {
    try {
      for (const msg of await messenger.receivedSince(since)) {
        if (seen.has(msg.id)) continue;
        seen.add(msg.id);
        since = msg.createdAt > since ? msg.createdAt : since;
        log(`reply ${msg.id.slice(-6)}: ${await handleReply(msg, messenger, gateway)}`);
      }
    } catch (e) {
      log(`reply loop error: ${e instanceof Error ? e.message : String(e)}`);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
