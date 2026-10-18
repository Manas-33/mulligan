import { describe, expect, it } from "vitest";
import { empty, heldActions, reduce, sessionList, timeline } from "../src/state.ts";
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
