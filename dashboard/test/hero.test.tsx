import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Hero } from "../src/components/Hero.tsx";
import type { Action, Session } from "../src/types.ts";

const session = (status: Session["status"] = "active") =>
  ({
    id: "ses_01M561Q6R04CJZXHXRCFFNM7C4",
    agent: { id: "claude_code", kind: "claude_code", display_name: "Claude Code" },
    mode: "live",
    started_at: "2026-10-17T23:04:00Z",
    ended_at: null,
    status,
  }) as Session;

const action = (label: Action["reversibility"]["label"], status: Action["status"] = "executed") =>
  ({ id: "act_1", system: "postgres", status, reversibility: { label, source: "policy", reason: null }, created_at: "2026-10-17T23:04:07Z" }) as Action;

const reads = (n: number) => Array.from({ length: n }, () => action("read_only"));

/** The hero's words, without markup. */
const text = (s: Session, actions: Action[], canRewind = true) =>
  renderToStaticMarkup(<Hero session={s} actions={actions} canRewind={canRewind} onRewind={() => {}} />)
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

describe("Hero", () => {
  it("counts only the changes, and says the reads quietly underneath", () => {
    const t = text(session(), [...reads(7), action("reversible")]);
    expect(t).toContain("The agent made 1 change. Undo with a mulligan.");
    expect(t).toContain("It also read data 7 times. Reads change nothing");
    expect(t).toContain("Undo 1 change");
  });
  it("reads once, and leaves the reads out when there were none", () => {
    const t = text(session(), [...reads(1), action("reversible"), action("compensable", "held")]);
    expect(t).toContain("The agent made 2 changes. Undo with a mulligan.");
    expect(t).toContain("It also read data once.");
    expect(text(session(), [action("reversible")])).not.toContain("read data");
  });
  it("has nothing to undo after only reads", () => {
    const t = text(session(), reads(3), false);
    expect(t).toContain("The agent made no changes. Nothing to undo.");
    expect(t).not.toContain("Undo ");
  });
  it("says what an undo took back and that the reads needed nothing", () => {
    const t = text(session("rewound"), [...reads(7), action("reversible", "reverted")], false);
    expect(t).toContain("Undone with a mulligan.");
    expect(t).toContain("1 change undone · 7 reads needed nothing");
    expect(t).not.toContain("Undo 1 change");
  });
});
