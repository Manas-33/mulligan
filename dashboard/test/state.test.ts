import { describe, expect, it } from "vitest";
import { empty, heldActions, latestRewind, reduce, sessionList, timeline } from "../src/state.ts";
import type { Action, MulliganEvent, Session } from "../src/types.ts";

const session = (id: string, started_at: string) => ({ id, started_at }) as Session;
const action = (id: string, status: Action["status"], session_id = "ses_a") => ({ id, status, session_id }) as Action;
const ev = (seq: number, event: string, data: unknown) => ({ type: "event" as const, event: { event, seq, ts: "t", data } as MulliganEvent });

describe("state", () => {
  it("loads a snapshot and orders sessions and timelines newest first", () => {
    const s = reduce(empty, {
      type: "snapshot",
      sessions: [session("ses_a", "2026-10-18T01:00:00Z"), session("ses_b", "2026-10-18T02:00:00Z")],
      actions: [action("act_01", "executed"), action("act_02", "held"), action("act_03", "executed", "ses_b")],
    });
    expect(sessionList(s).map((x) => x.id)).toEqual(["ses_b", "ses_a"]);
    expect(timeline(s, "ses_a").map((x) => x.id)).toEqual(["act_02", "act_01"]);
    expect(heldActions(s).map((x) => x.id)).toEqual(["act_02"]);
  });

  it("replaces whole objects from events and ignores replayed seqs", () => {
    let s = reduce(empty, ev(5, "action.held", action("act_01", "held")));
    s = reduce(s, ev(6, "action.approved", action("act_01", "approved")));
    s = reduce(s, ev(6, "action.held", action("act_01", "held"))); // replay after reconnect
    expect(s.actions.act_01!.status).toBe("approved");
    expect(s.lastSeq).toBe(6);
  });

  it("handles session and rewind.step events", () => {
    let s = reduce(empty, ev(1, "session.started", session("ses_a", "2026-10-18T01:00:00Z")));
    s = reduce(s, ev(2, "rewind.step", { rewind_job_id: "rwd_1", step: {}, action: action("act_01", "reverted") }));
    s = reduce(s, ev(3, "rewind.completed", { id: "rwd_1" }));
    expect(Object.keys(s.sessions)).toEqual(["ses_a"]);
    expect(s.actions.act_01!.status).toBe("reverted");
    expect(s.lastSeq).toBe(3);
  });
});

describe("rewinds", () => {
  const job = (id: string, status: string, steps: unknown[] = []) =>
    ({ id, status, dry_run: false, scope: { session_id: "ses_a" }, steps, verification: {} }) as never;
  const step = (action_id: string, result: string) => ({ action_id, result }) as never;

  it("tracks a run from start, through steps, to completion", () => {
    let s = reduce(empty, { type: "rewind", job: job("rwd_1", "running") });
    s = reduce(s, ev(10, "rewind.step", { rewind_job_id: "rwd_1", step: step("act_02", "restored"), action: action("act_02", "reverted") }));
    s = reduce(s, ev(11, "rewind.step", { rewind_job_id: "rwd_1", step: step("act_01", "compensated"), action: action("act_01", "reverted") }));
    expect(s.rewinds.rwd_1!.steps.map((x) => x.action_id)).toEqual(["act_02", "act_01"]);
    expect(s.actions.act_01!.status).toBe("reverted");
    s = reduce(s, ev(12, "rewind.completed", job("rwd_1", "completed", [step("act_02", "restored"), step("act_01", "compensated")])));
    expect(s.rewinds.rwd_1!.status).toBe("completed");
  });

  it("finds the newest rewind for a session and keeps checksum events", () => {
    let s = reduce(empty, { type: "rewind", job: job("rwd_1", "completed") });
    s = reduce(s, { type: "rewind", job: job("rwd_2", "running") });
    expect(latestRewind(s, "ses_a")!.id).toBe("rwd_2");
    expect(latestRewind(s, "ses_b")).toBeUndefined();
    s = reduce(s, ev(3, "checksum.verified", { system: "postgres", expected: "a", actual: "a", ok: true, rewind_job_id: "rwd_2" }));
    expect(s.checks).toHaveLength(1);
  });
});
