import { describe, expect, it } from "vitest";
import type { Action, RewindJob, RewindStep } from "../src/types.ts";
import {
  duration,
  fieldLabel,
  groupHash,
  laneCaption,
  planCounts,
  planSentence,
  position,
  rewindSummary,
  actionCounts,
  labelText,
  showValue,
  stepKind,
  timeLeft,
  undoableFrom,
  isUndoable,
  unfinishedSteps,
  describeProblem,
} from "../src/view.ts";

const step = (over: Partial<RewindStep>): RewindStep => ({
  action_id: "act_1",
  system: "postgres",
  tool: "pg_update_rows",
  result: "pending",
  kept_fields: [],
  changes: [],
  changes_truncated: false,
  detail: null,
  ...over,
});

const action = (over: Partial<Action>) =>
  ({
    id: "act_1",
    system: "postgres",
    status: "executed",
    reversibility: { label: "reversible", source: "policy", reason: null },
    ...over,
  }) as Action;

const read = (over: Partial<Action>) => action({ reversibility: { label: "read_only", source: "policy", reason: null }, ...over });

const job = (steps: RewindStep[], verification: RewindJob["verification"] = {}) =>
  ({ id: "rwd_1", status: "completed", steps, verification }) as unknown as RewindJob;

describe("stepKind", () => {
  it("files held and skipped actions as held", () => {
    expect(stepKind(step({}), action({ status: "held" }))).toBe("held");
    expect(stepKind(step({ result: "skipped" }), action({}))).toBe("held");
  });
  it("files kept fields before compensation", () => {
    expect(stepKind(step({ kept_fields: ["postgres:products/42/price"] }), action({}))).toBe("kept");
  });
  it("uses the reversibility label for compensation", () => {
    const a = action({ reversibility: { label: "compensable", source: "policy", reason: null } });
    expect(stepKind(step({}), a)).toBe("compensate");
    expect(stepKind(step({}), action({}))).toBe("restore");
  });
});

describe("planSentence", () => {
  it("joins the parts in plain words", () => {
    const actions = { a: action({ id: "a" }), b: action({ id: "b" }), h: action({ id: "h", status: "held" }) };
    const j = job([step({ action_id: "a" }), step({ action_id: "b", kept_fields: ["x"] }), step({ action_id: "h" })]);
    expect(planSentence(planCounts(j, actions))).toBe(
      "1 change goes back to how it was, 1 human edit stays and 1 held action is refused.",
    );
  });
  it("says so when there is nothing to undo", () => {
    expect(planSentence({ restore: 0, compensate: 0, kept: 0, held: 0 })).toBe("There's nothing to undo in this session.");
  });
});

describe("rewindSummary", () => {
  it("names the systems whose checksums matched", () => {
    const j = job([step({})], {
      postgres: { expected: "a", actual: "a", ok: true },
      github: { expected: "b", actual: "b", ok: true },
      notion: { expected: "c", actual: "d", ok: false },
    });
    expect(rewindSummary(j, { act_1: action({}) }, "round 7f3a")).toBe(
      "Rewound round 7f3a. 1 change undone. Postgres and GitHub checksums match. Notion did not match.",
    );
  });
});

describe("formatting", () => {
  it("places a time on the span and clamps it", () => {
    expect(position("2026-10-17T23:05:00Z", "2026-10-17T23:04:00Z", "2026-10-17T23:06:00Z")).toBe(0.5);
    expect(position("2026-10-17T23:10:00Z", "2026-10-17T23:04:00Z", "2026-10-17T23:06:00Z")).toBe(1);
    expect(position("2026-10-17T23:05:00Z", "2026-10-17T23:05:00Z", "2026-10-17T23:05:00Z")).toBe(1);
  });
  it("formats durations and countdowns", () => {
    expect(duration("2026-10-17T22:40:00Z", "2026-10-17T22:41:12Z")).toBe("1m 12s");
    expect(duration("2026-10-17T22:40:00Z", "2026-10-17T22:40:09Z")).toBe("9s");
    const now = Date.parse("2026-10-17T23:15:00Z");
    expect(timeLeft("2026-10-17T23:19:28Z", now)).toBe("4:28 left");
    expect(timeLeft("2026-10-17T23:14:00Z", now)).toBe("expired");
    expect(timeLeft(null, now)).toBeNull();
  });
  it("groups full hashes and leaves short ones alone", () => {
    expect(groupHash("4f53cda18c2baa0c0354bb5f9a3ecbe5")).toBe("4f53 cda1 8c2b aa0c");
    expect(groupHash("9f2c1a…e41b")).toBe("9f2c1a…e41b");
    expect(groupHash(null)).toBe("–");
  });
  it("labels fields and values for the plan", () => {
    expect(fieldLabel("postgres:products/42/price")).toBe("products/42 · price");
    expect(fieldLabel("postgres:legacy_pricing")).toBe("legacy_pricing");
    expect(showValue(null)).toBe("gone");
    expect(showValue({ rows: 120 })).toBe("120 rows");
    expect(showValue(45)).toBe("45");
    expect(showValue({ id: 7, name: "Ada Park" })).toBe("Ada Park’s row");
    expect(showValue({ sku: "x" })).toBe("a row");
  });
  it("captions a lane by what happened", () => {
    expect(laneCaption("postgres", [action({}), action({}), action({ status: "held" })])).toBe("2 writes · 1 held");
    expect(laneCaption("email", [action({ status: "held" })])).toBe("1 held");
  });
  it("counts reads apart from the writes", () => {
    expect(laneCaption("postgres", [action({}), read({}), read({}), action({ status: "held" })])).toBe("1 write · 2 reads · 1 held");
    expect(laneCaption("mulligan", [read({})])).toBe("1 read");
    expect(laneCaption("email", [action({ status: "rejected" })])).toBe("1 refused");
  });
});

describe("undoableFrom", () => {
  it("takes the action itself and everything after it that can still be undone", () => {
    const at = (id: string, created_at: string, status: Action["status"] = "executed") => action({ id, created_at, status });
    const list = [
      at("a4", "2026-10-17T23:04:35Z", "held"),
      at("a3", "2026-10-17T23:04:21Z", "reverted"),
      at("a2", "2026-10-17T23:04:14Z"),
      at("a1", "2026-10-17T23:04:07Z"),
    ];
    expect(undoableFrom(list, "2026-10-17T23:04:14Z").map((a) => a.id)).toEqual(["a4", "a2"]);
    expect(undoableFrom(list, "2026-10-17T23:05:00Z")).toEqual([]);
  });

  it("matches the gateway: no kept-edit conflicts, no reads", () => {
    const read = action({ id: "r", status: "executed", reversibility: { label: "read_only", source: "policy", reason: null } });
    const kept = action({ id: "k", status: "conflict" });
    expect(isUndoable(read)).toBe(false);
    expect(isUndoable(kept)).toBe(false);
    expect(isUndoable(action({ id: "w", status: "executed" }))).toBe(true);
    expect(undoableFrom([read, kept], "2000-01-01T00:00:00Z")).toEqual([]);
  });

  it("matches the gateway: a call still running is waited for and undone", () => {
    expect(isUndoable(action({ status: "pending" }))).toBe(true);
    expect(isUndoable(action({ status: "executing" }))).toBe(true);
    const read = { label: "read_only", source: "policy", reason: null } as const;
    expect(isUndoable(action({ status: "executing", reversibility: read }))).toBe(false);
  });

  it("matches the gateway: a failed undo step is tried again", () => {
    expect(isUndoable(action({ status: "revert_failed" }))).toBe(true);
  });

  it("matches the gateway: a failed call counts only if its change may have landed", () => {
    const inverse = { kind: "github.delete_comment", payload: {} };
    expect(isUndoable(action({ status: "failed", executed_at: null, inverse: null }))).toBe(false);
    expect(isUndoable(action({ status: "failed", executed_at: null, inverse }))).toBe(true);
    // Rewound since (reverting -> reverted): done, like any other reverted action.
    expect(isUndoable(action({ status: "reverted", executed_at: null, inverse }))).toBe(false);
    // Its undo step failed: tried again.
    expect(isUndoable(action({ status: "revert_failed", executed_at: null, inverse }))).toBe(true);
  });
});

describe("undoableFrom and reads", () => {
  it("never takes back a read: it changed nothing", () => {
    const list = [read({ id: "r1", created_at: "2026-10-17T23:04:20Z" }), action({ id: "a1", created_at: "2026-10-17T23:04:10Z" })];
    expect(undoableFrom(list, "2026-10-17T23:04:00Z").map((a) => a.id)).toEqual(["a1"]);
    expect(undoableFrom(list, "2026-10-17T23:04:15Z")).toEqual([]);
  });
});

describe("actionCounts", () => {
  it("splits reads from changes, counting held ones as changes", () => {
    const list = [read({}), read({}), read({}), action({}), action({ status: "held" })];
    expect(actionCounts(list)).toEqual({ reads: 3, changes: 2, left: 2, undone: 0, refused: 0 });
  });
  it("counts what an undo took back and leaves the reads alone", () => {
    const list = [read({}), action({ status: "reverted" }), action({ status: "conflict" }), action({ status: "rejected" })];
    expect(actionCounts(list)).toEqual({ reads: 1, changes: 3, left: 0, undone: 2, refused: 1 });
  });
  it("handles a session of only reads", () => {
    expect(actionCounts([read({}), read({})])).toEqual({ reads: 2, changes: 0, left: 0, undone: 0, refused: 0 });
  });
});

describe("labelText", () => {
  it("says the reversibility label in plain words", () => {
    expect(labelText("read_only")).toBe("read only");
    expect(labelText("compensable")).toBe("can be offset, not undone");
    expect(labelText("irreversible")).toBe("can't be undone");
  });
});

describe("failed and partial rewinds", () => {
  it("lists failed steps, and on a stopped run the ones never reached", () => {
    const partial = { ...job([step({ action_id: "a", result: "failed" }), step({ action_id: "b", result: "restored" })], {}), status: "partial" as const };
    expect(unfinishedSteps(partial).map((s) => s.action_id)).toEqual(["a"]);
    const stopped = { ...job([step({ action_id: "a", result: "failed" }), step({ action_id: "b", result: "pending" })], {}), status: "failed" as const };
    expect(unfinishedSteps(stopped).map((s) => s.action_id)).toEqual(["a", "b"]);
  });

  it("doesn't count what didn't come back as undone", () => {
    const j = { ...job([step({ action_id: "a", result: "failed" }), step({ action_id: "b", result: "restored" })], {}), status: "partial" as const };
    expect(rewindSummary(j, { a: action({ id: "a" }), b: action({ id: "b" }) }, "round 7f3a")).toBe(
      "Rewound round 7f3a. 1 change undone. 1 change didn't come back.",
    );
  });
});

describe("describeProblem", () => {
  it("sorts failures into unauthorized, gateway and unreachable", () => {
    expect(describeProblem({ status: 401, detail: "missing or invalid token" })).toEqual({ kind: "unauthorized", status: 401, detail: "missing or invalid token" });
    expect(describeProblem({ status: 422, detail: [{ msg: "scope needs at least one field" }] }).detail).toBe("scope needs at least one field");
    expect(describeProblem({ status: 500, detail: null }).kind).toBe("gateway");
    expect(describeProblem(new TypeError("Failed to fetch"))).toEqual({ kind: "unreachable", detail: "Failed to fetch" });
  });
});
