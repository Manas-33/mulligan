// Texts the approver a one-line summary when a rewind finishes. The service watches the gateway's
// rewind jobs (GET /api/rewind) instead of the gateway calling us, so no core change is needed.
// Each job is texted at most once: the idempotency key is the job id, the same key POST
// /notify/summary uses, so even a restart or a manual call can't double-text.

import type { Gateway } from "./gateway.ts";
import { deliver, summaryKey } from "./notify.ts";
import type { Messenger } from "./photon.ts";
import type { RewindJob, System } from "./types.ts";

const FINISHED = new Set<RewindJob["status"]>(["completed", "partial", "failed"]);

const NAMES: Record<System, string> = {
  postgres: "Postgres",
  github: "GitHub",
  notion: "Notion",
  email: "Email",
  payments: "Payments",
  upstream: "other tools",
  mulligan: "Mulligan",
};

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const join = (xs: string[]) => (xs.length < 2 ? (xs[0] ?? "") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);

/** "7f3a" from "ses_01M561Q6R04CJZXHXRCFFNM7C4": the same short id the dashboard shows. */
export const shortId = (id: string) => id.replace(/^[a-z]+_/, "").slice(-4).toLowerCase();

/** The summary text, from the job alone (step results and checksums). Under ~300 characters. */
export function summaryText(job: RewindJob): string {
  const count = (r: RewindJob["steps"][number]["result"]) => job.steps.filter((s) => s.result === r).length;
  const undone = count("restored") + count("compensated");
  const refused = count("skipped");
  const kept = count("kept_human_edit");
  const failed = count("failed");
  const checks = Object.entries(job.verification);
  const ok = checks.filter(([, v]) => v.ok === true).map(([s]) => NAMES[s as System] ?? s);
  const bad = checks.filter(([, v]) => v.ok === false).map(([s]) => NAMES[s as System] ?? s);

  const round = `round ${shortId(job.scope.session_id ?? job.id)}`;
  const opening =
    job.status === "failed"
      ? `The rewind of ${round} stopped${job.error ? `: ${job.error}` : "."}`
      : job.status === "partial"
        ? `Rewound most of ${round}.`
        : `Rewound ${round}.`;
  const bits = [opening.endsWith(".") ? opening : `${opening}.`];
  const did: string[] = [];
  if (undone) did.push(`${plural(undone, "change")} undone`);
  if (refused) did.push(`${plural(refused, "held action")} refused`);
  if (kept) did.push(`kept ${plural(kept, "human edit")}`);
  if (did.length) bits.push(`${join(did).replace(/^./, (c) => c.toUpperCase())}.`);
  if (failed) bits.push(`${plural(failed, "step")} failed, see the dashboard.`);
  if (ok.length) bits.push(`${join(ok)} ${ok.length === 1 ? "checksum matches" : "checksums match"}.`);
  if (bad.length) bits.push(`${join(bad)} did not match.`);
  const text = bits.join(" ");
  return text.length > 300 ? `${text.slice(0, 297)}…` : text;
}

export type SummaryLog = (line: string) => void;

/** Handle one batch of jobs: text each newly finished run once. Returns what happened, for tests. */
export async function sendFinished(
  jobs: RewindJob[],
  messenger: Messenger,
  to: string,
  state: { since: string; done: Set<string> },
): Promise<string[]> {
  const out: string[] = [];
  for (const job of jobs) {
    if (job.dry_run || !FINISHED.has(job.status) || state.done.has(job.id)) continue;
    // Only runs that finish after startup; older ones were (or weren't) texted by an earlier run.
    const finishedAt = job.completed_at ?? job.started_at ?? job.created_at;
    // Compare instants, not strings: the gateway may write "+00:00" where we write "Z".
    if (Date.parse(finishedAt) < Date.parse(state.since)) {
      state.done.add(job.id);
      continue;
    }
    const reply = await deliver(messenger, to, `Mulligan: ${summaryText(job)}`, summaryKey(job.id));
    // A 409 means the approver hasn't texted the line yet; retrying every poll won't fix that.
    state.done.add(job.id);
    out.push(`${shortId(job.id)}: ${reply.status === 202 ? "sent" : "not reachable"}`);
  }
  return out;
}

/** Poll the gateway's rewind jobs until `signal` aborts. */
export async function summaryLoop(
  gateway: Gateway,
  messenger: Messenger,
  to: string,
  opts: { intervalMs?: number; signal?: AbortSignal; log?: SummaryLog; now?: () => Date } = {},
): Promise<void> {
  const { intervalMs = 2000, signal, log = console.log, now = () => new Date() } = opts;
  const state = { since: now().toISOString(), done: new Set<string>() };
  while (!signal?.aborted) {
    try {
      for (const line of await sendFinished(await gateway.rewinds(20), messenger, to, state)) log(`summary ${line}`);
    } catch (e) {
      log(`summary loop error: ${e instanceof Error ? e.message : String(e)}`);
    }
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}
