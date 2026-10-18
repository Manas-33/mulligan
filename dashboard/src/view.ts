// Pure helpers that turn contract objects into what the screens show. No React here, so it's all
// unit-tested (test/view.test.ts).

import type { Action, RewindJob, RewindStep, System } from "./types.ts";

/** How a rewind step is filed on the plan and the printout. */
export type StepKind = "restore" | "compensate" | "kept" | "held";

/** Held actions never ran, so a rewind refuses them instead of undoing anything. */
export function stepKind(step: RewindStep, action: Action | undefined): StepKind {
  if (action?.status === "held" || action?.status === "rejected" || step.result === "skipped") return "held";
  if (step.result === "kept_human_edit" || step.kept_fields.length > 0) return "kept";
  if (step.result === "compensated" || action?.reversibility.label === "compensable") return "compensate";
  return "restore";
}

export type PlanCounts = Record<StepKind, number>;

export function planCounts(job: RewindJob, actions: Record<string, Action>): PlanCounts {
  const counts: PlanCounts = { restore: 0, compensate: 0, kept: 0, held: 0 };
  for (const step of job.steps) counts[stepKind(step, actions[step.action_id])]++;
  return counts;
}

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** The one-sentence summary at the top of the plan. */
export function planSentence(c: PlanCounts): string {
  const parts: string[] = [];
  if (c.restore) parts.push(`${plural(c.restore, "change")} ${c.restore === 1 ? "goes" : "go"} back to how ${c.restore === 1 ? "it was" : "they were"}`);
  if (c.compensate) parts.push(`${plural(c.compensate, "change")} ${c.compensate === 1 ? "is" : "are"} offset`);
  if (c.kept) parts.push(`${plural(c.kept, "human edit")} ${c.kept === 1 ? "stays" : "stay"}`);
  if (c.held) parts.push(`${plural(c.held, "held action")} ${c.held === 1 ? "is" : "are"} refused`);
  if (parts.length === 0) return "There's nothing to undo in this session.";
  const last = parts.pop()!;
  return `${parts.length ? `${parts.join(", ")} and ${last}` : last}.`.replace(/^./, (s) => s.toUpperCase());
}

/** Text for the summary after a finished rewind (the same words the iMessage summary uses). */
export function rewindSummary(job: RewindJob, actions: Record<string, Action>, label: string): string {
  const left = new Set(unfinishedSteps(job).map((s) => s.action_id));
  const c = planCounts({ ...job, steps: job.steps.filter((s) => !left.has(s.action_id)) }, actions);
  const checks = Object.entries(job.verification);
  const ok = checks.filter(([, v]) => v.ok === true).map(([s]) => systemName(s as System));
  const bad = checks.filter(([, v]) => v.ok === false).map(([s]) => systemName(s as System));
  const bits = [job.status === "failed" ? `The rewind of ${label} stopped early.` : `Rewound ${label}.`];
  if (c.restore + c.compensate) bits.push(`${plural(c.restore + c.compensate, "change")} undone.`);
  if (c.held) bits.push(`${plural(c.held, "held action")} refused.`);
  if (c.kept) bits.push(`Kept ${plural(c.kept, "human edit")}.`);
  if (left.size) bits.push(`${plural(left.size, "change")} didn't come back.`);
  if (ok.length) bits.push(`${listJoin(ok)} ${ok.length === 1 ? "checksum matches" : "checksums match"}.`);
  if (bad.length) bits.push(`${listJoin(bad)} did not match.`);
  return bits.join(" ");
}

export const listJoin = (xs: string[]) => (xs.length < 2 ? (xs[0] ?? "") : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);

const NAMES: Record<System, string> = {
  postgres: "Postgres",
  github: "GitHub",
  notion: "Notion",
  email: "Email",
  payments: "Payments",
  upstream: "Other tools",
  mulligan: "Mulligan",
};
export const systemName = (s: System) => NAMES[s] ?? s;

/** Lane order on the timeline. */
export const SYSTEM_ORDER: System[] = ["postgres", "github", "notion", "email", "payments", "upstream", "mulligan"];

export function countBySystem(actions: Action[]): Partial<Record<System, number>> {
  const out: Partial<Record<System, number>> = {};
  for (const a of actions) out[a.system] = (out[a.system] ?? 0) + 1;
  return out;
}

/** What a lane says under its name: "15 writes", "40 closed", "1 held". */
export function laneCaption(system: System, actions: Action[]): string {
  const held = actions.filter((a) => a.status === "held").length;
  const done = actions.length - held;
  const verb: Partial<Record<System, string>> = { postgres: "write", github: "change", notion: "page", email: "email", payments: "charge" };
  const parts = [];
  if (done) parts.push(plural(done, verb[system] ?? "call"));
  if (held) parts.push(`${held} held`);
  return parts.join(" · ");
}

/** 0..1 position of `at` between `start` and `end`; a zero-length span puts everything at the end. */
export function position(at: string, start: string, end: string): number {
  const a = Date.parse(at);
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (!(e > s)) return 1;
  return Math.min(1, Math.max(0, (a - s) / (e - s)));
}

/** "3m 40s", "58s", "1h 02m". */
export function duration(fromIso: string, toIso: string): string {
  const s = Math.max(0, Math.round((Date.parse(toIso) - Date.parse(fromIso)) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** "4:12 left" until a hold expires, or "expired". */
export function timeLeft(expiresIso: string | null | undefined, now: number): string | null {
  if (!expiresIso) return null;
  const s = Math.round((Date.parse(expiresIso) - now) / 1000);
  if (s <= 0) return "expired";
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} left`;
}

/** Group a hash into fours so expected and actual can be compared by eye. Already-shortened hashes stay as they are. */
export function groupHash(hash: string | null | undefined): string {
  if (!hash) return "–";
  if (hash.includes("…") || hash.length < 16) return hash;
  return (hash.slice(0, 16).match(/.{4}/g) ?? []).join(" ");
}

/** A field value as the plan shows it. */
export function showValue(v: unknown): string {
  if (v === null || v === undefined) return "gone";
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  if (typeof v === "object" && v && "rows" in v) return `${(v as { rows: unknown }).rows} rows`;
  // A whole row (a deleted customer, say): name it rather than dumping it.
  if (typeof v === "object" && v && !Array.isArray(v)) {
    const r = v as Record<string, unknown>;
    const name = r.name ?? r.title ?? r.email ?? r.id;
    return name === undefined ? "a row" : `${String(name)}’s row`;
  }
  return JSON.stringify(v);
}

/** "postgres:products/42/price" -> "products/42 · price" for the plan's field column. */
export function fieldLabel(field: string): string {
  const path = field.includes(":") ? field.slice(field.indexOf(":") + 1) : field;
  const parts = path.split("/");
  if (parts.length < 3) return parts.join(" · ");
  return `${parts.slice(0, -1).join("/")} · ${parts.at(-1)}`;
}

export const shortId = (id: string) => id.replace(/^[a-z]+_/, "").slice(-4).toLowerCase();

/** Statuses a rewind can still do something about (undo it, or refuse it while held). */
export const UNDOABLE = new Set<Action["status"]>(["executed", "held", "approved", "conflict"]);

/** Undoable actions created at or after `since`: what "Rewind to here" would take back. */
export const undoableFrom = (actions: Action[], since: string): Action[] =>
  actions.filter((a) => UNDOABLE.has(a.status) && Date.parse(a.created_at) >= Date.parse(since));

/** Why a gateway call failed, sorted into what the screens can say something useful about. */
export type Problem = { kind: "unauthorized" | "unreachable" | "gateway"; status?: number; detail: string };

/** Accepts ApiError's shape ({status, detail}) without importing the client, so this stays pure. */
export function describeProblem(e: unknown): Problem {
  if (e && typeof e === "object" && "status" in e && typeof e.status === "number") {
    const status = e.status;
    const detail = detailText((e as { detail?: unknown }).detail) || `The gateway answered ${status}.`;
    return { kind: status === 401 || status === 403 ? "unauthorized" : "gateway", status, detail };
  }
  // fetch rejects with a TypeError when nothing answers (refused, DNS, CORS).
  return { kind: "unreachable", detail: e instanceof Error ? e.message : String(e) };
}

/** A FastAPI `detail` as one sentence: a string, or a 422's list of {msg}. */
export function detailText(detail: unknown): string {
  if (detail === null || detail === undefined) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : String(d))).join("; ");
  return JSON.stringify(detail);
}

/** Steps a finished run didn't put back: they failed, or the run stopped before reaching them. */
export const unfinishedSteps = (job: RewindJob): RewindStep[] =>
  job.status === "completed" ? job.steps.filter((s) => s.result === "failed") : job.steps.filter((s) => s.result === "failed" || s.result === "pending");
