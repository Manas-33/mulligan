import assert from "node:assert/strict";
import { test } from "node:test";
import { sendFinished, summaryLoop, summaryText } from "../src/summaries.ts";
import type { RewindJob, RewindStep } from "../src/types.ts";
import { FakeGateway, FakeMessenger } from "./fakes.ts";

const TO = "approver@icloud.com";

const step = (result: RewindStep["result"], n: number): RewindStep => ({
  action_id: `act_01M561R2302EME3JAF7RBZWM${String(n).padStart(2, "0")}`,
  system: "postgres",
  tool: "pg_update_rows",
  result,
  kept_fields: result === "kept_human_edit" ? ["postgres:products/42/price"] : [],
  changes: [],
  changes_truncated: false,
  detail: null,
});

function job(over: Partial<RewindJob> = {}): RewindJob {
  return {
    id: "rwd_01M561RFRGVASABYV8XG40DDX0",
    scope: { session_id: "ses_01M561Q6R04CJZXHXRCFFNM7C4" },
    dry_run: false,
    status: "completed",
    steps: [step("restored", 1), step("restored", 2), step("kept_human_edit", 3), step("skipped", 4)],
    verification: {
      postgres: { expected: "9f2c", actual: "9f2c", ok: true },
      github: { expected: "03b7", actual: "03b7", ok: true },
      notion: { expected: "e5a9", actual: "e5a9", ok: true },
    },
    requested_by: "dashboard",
    error: null,
    created_at: "2026-10-18T01:00:00Z",
    started_at: "2026-10-18T01:00:00Z",
    completed_at: "2026-10-18T01:00:09Z",
    ...over,
  } as RewindJob;
}

test("summary says what came back and which checksums match", () => {
  assert.equal(
    summaryText(job()),
    "Rewound round m7c4. 2 changes undone, 1 held action refused and kept 1 human edit. Postgres, GitHub and Notion checksums match.",
  );
});

test("partial and failed runs say so", () => {
  const partial = job({ status: "partial", verification: { postgres: { expected: "a", actual: "b", ok: false } } });
  assert.match(summaryText(partial), /^Rewound most of round m7c4\./);
  assert.match(summaryText(partial), /Postgres did not match\.$/);
  const failed = job({ status: "failed", error: "github is unreachable", steps: [step("failed", 1)], verification: {} });
  assert.equal(summaryText(failed), "The rewind of round m7c4 stopped: github is unreachable. 1 step failed, see the dashboard.");
});

test("a finished run is texted once, with the job id as the idempotency key", async () => {
  const m = new FakeMessenger();
  const state = { since: "2026-10-18T00:59:00Z", done: new Set<string>() };
  assert.deepEqual(await sendFinished([job()], m, TO, state), ["ddx0: sent"]);
  assert.deepEqual(await sendFinished([job()], m, TO, state), []);
  assert.equal(m.sent.length, 1);
  assert.equal(m.sent[0]!.key, "mulligan-summary-rwd_01M561RFRGVASABYV8XG40DDX0");
  assert.match(m.texts()[0]!, /^Mulligan: Rewound round m7c4\./);
});

test("dry runs, running jobs and runs from before startup are skipped", async () => {
  const m = new FakeMessenger();
  const state = { since: "2026-10-18T02:00:00Z", done: new Set<string>() };
  const jobs = [
    job({ id: "rwd_01M561RFRGVASABYV8XG40DD01", dry_run: true, status: "planned", completed_at: null }),
    job({ id: "rwd_01M561RFRGVASABYV8XG40DD02", status: "running", completed_at: null }),
    job({ id: "rwd_01M561RFRGVASABYV8XG40DD03" }), // finished at 01:00, before startup
  ];
  assert.deepEqual(await sendFinished(jobs, m, TO, state), []);
  assert.equal(m.sent.length, 0);
  // Offsets other than Z compare by time, not by text.
  const offset = job({ id: "rwd_01M561RFRGVASABYV8XG40DD04", completed_at: "2026-10-18T01:59:59+00:00" });
  assert.deepEqual(await sendFinished([offset], m, TO, state), []);
  // The running job is texted once it finishes.
  const finished = job({ id: "rwd_01M561RFRGVASABYV8XG40DD02", completed_at: "2026-10-18T02:00:05Z" });
  assert.deepEqual(await sendFinished([finished], m, TO, state), ["dd02: sent"]);
});

test("an unreachable approver is logged once, not retried every poll", async () => {
  const m = new FakeMessenger();
  m.refuseSend = true;
  const state = { since: "2026-10-18T00:00:00Z", done: new Set<string>() };
  assert.deepEqual(await sendFinished([job()], m, TO, state), ["ddx0: not reachable"]);
  assert.deepEqual(await sendFinished([job()], m, TO, state), []);
});

test("the loop polls the gateway and keeps going after an error", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway();
  let calls = 0;
  g.rewinds = async () => {
    calls++;
    if (calls === 1) throw new Error("gateway down");
    return [job({ completed_at: "2026-10-18T03:00:01Z" })];
  };
  const stop = new AbortController();
  const lines: string[] = [];
  const done = summaryLoop(g, m, TO, {
    intervalMs: 5,
    signal: stop.signal,
    log: (l) => {
      lines.push(l);
      if (l.startsWith("summary ddx0")) stop.abort();
    },
    now: () => new Date("2026-10-18T03:00:00Z"),
  });
  await done;
  assert.deepEqual(lines, ["summary loop error: gateway down", "summary ddx0: sent"]);
  assert.equal(m.sent.length, 1);
});

test("loop errors reach onError once per failing streak", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway();
  // down, down, down, up, down: two streaks, so two reports.
  const script = ["down", "down", "down", "up", "down"];
  let calls = 0;
  const stop = new AbortController();
  g.rewinds = async () => {
    const step = script[calls++];
    if (step === undefined) {
      stop.abort();
      return [];
    }
    if (step === "down") throw new Error("gateway down");
    return [];
  };
  const reported: unknown[] = [];
  await summaryLoop(g, m, TO, { intervalMs: 1, signal: stop.signal, log: () => {}, onError: (e) => reported.push(e) });
  assert.equal(reported.length, 2);
  assert.match(String(reported[0]), /gateway down/);
});
