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
  it("says the reads quietly and counts only the shots", () => {
    const t = text(session(), [...reads(7), action("reversible")]);
    expect(t).toContain("The agent read the green 7 times and took 1 shot.");
    expect(t).toContain("Rewind 1 shot");
  });
  it("reads once, and leaves the reads out when there were none", () => {
    expect(text(session(), [...reads(1), action("reversible"), action("compensable", "held")])).toContain(
      "The agent read the green once and took 2 shots.",
    );
    const t = text(session(), [action("reversible")]);
    expect(t).toContain("The agent took 1 shot.");
    expect(t).not.toContain("read the green");
  });
  it("has nothing to take back after only reads", () => {
    const t = text(session(), reads(3), false);
    expect(t).toContain("The agent read the green 3 times and took no shots. Nothing to take back.");
    expect(t).not.toContain("Rewind");
  });
  it("says what a rewind took back and that the reads stayed put", () => {
    const t = text(session("rewound"), [...reads(7), action("reversible", "reverted")], false);
    expect(t).toContain("You took the mulligan.");
    expect(t).toContain("1 shot rewound · 7 reads left as they were");
    expect(t).not.toContain("Rewind 1 shot");
  });
});
