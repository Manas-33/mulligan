// Turns the approver's iMessage replies ("YES 4821" / "NO 4821") into gateway decisions.
// Stateless: the code is looked up among the gateway's held actions, so a restart loses nothing.

import { GatewayError, type Gateway } from "./gateway.ts";
import { parseReply, sameHandle } from "./parse.ts";
import type { InboundText, Messenger } from "./photon.ts";

export type ReplyLog = (line: string) => void;

const CLOCK_SKEW_MS = 60_000;
/** Where loop errors go besides the log (Sentry in main.ts). */
export type OnError = (error: unknown) => void;

/** Handle one inbound text. Returns what happened, for logs and tests. Only the approver's texts
 * count: anyone can text the shared line, and a reply to a stranger would confirm codes. */
export async function handleReply(
  msg: InboundText,
  messenger: Messenger,
  gateway: Gateway,
  approver: string,
): Promise<string> {
  if (!sameHandle(msg.from, approver)) return "not-approver";
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
  const requestedAt = action?.approval?.requested_at;
  // Photon's clock and the gateway's can disagree by a little: allow a minute.
  if (action && requestedAt && Date.parse(msg.createdAt) < Date.parse(requestedAt) - CLOCK_SKEW_MS) {
    // Sent before this request went out: a late or repeated answer to an earlier hold that had
    // the same code. Saying nothing is safer than deciding the new one.
    return "stale";
  }
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
          ? `Approved, but it didn't run (${after.error ?? "see the dashboard"}).`
          : "Approved. It's running now."
        : "Rejected. It won't run.";
    await say(`${verdict} (${action.tool}, ${action.id.slice(-6)})`);
    return parsed.decision;
  } catch (e) {
    if (e instanceof GatewayError && (e.status === 409 || e.status === 404)) {
      await say(`Code ${parsed.code} was already decided.`);
      return "already-decided";
    }
    // Never leave the approver hanging: say it didn't go through, then let the loop log it.
    await say(`Couldn't record that (Mulligan error). Decide it in the dashboard instead.`);
    throw e;
  }
}

/** Pass on the first error of a failing streak (and any different error), not one per poll: a
 * gateway that's down for an hour shouldn't become thousands of Sentry events. */
export function streak(onError: OnError) {
  let last: string | null = null;
  return {
    ok: () => void (last = null),
    failed: (e: unknown) => {
      const key = e instanceof Error ? `${e.name}: ${e.message}` : String(e);
      if (key !== last) onError(e);
      last = key;
    },
  };
}

/** Poll for replies until `signal` aborts. Each message is handled once. */
export async function replyLoop(
  messenger: Messenger,
  gateway: Gateway,
  approver: string,
  opts: { intervalMs?: number; signal?: AbortSignal; log?: ReplyLog; onError?: OnError; now?: () => Date } = {},
): Promise<void> {
  const { intervalMs = 1000, signal, log = console.log, onError = () => {}, now = () => new Date() } = opts;
  const seen = new Set<string>();
  let since = now().toISOString(); // replies sent before startup are not ours to decide
  const report = streak(onError);
  while (!signal?.aborted) {
    try {
      for (const msg of await messenger.receivedSince(since)) {
        if (seen.has(msg.id)) continue;
        seen.add(msg.id);
        since = msg.createdAt > since ? msg.createdAt : since;
        log(`reply ${msg.id.slice(-6)}: ${await handleReply(msg, messenger, gateway, approver)}`);
      }
      report.ok();
    } catch (e) {
      log(`reply loop error: ${e instanceof Error ? e.message : String(e)}`);
      report.failed(e);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
