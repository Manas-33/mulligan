import assert from "node:assert/strict";
import { test } from "node:test";
import { GatewayError } from "../src/gateway.ts";
import { handleReply, replyLoop } from "../src/replies.ts";
import { FakeGateway, FakeMessenger, heldAction, inbound } from "./fakes.ts";

test("YES <code> approves the matching held action", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("1111", 1), heldAction("4821", 2)]);
  assert.equal(await handleReply(inbound("m1", "YES 4821"), m, g), "approve");
  assert.equal(g.decisions[0]!.actionId, heldAction("4821", 2).id);
  assert.deepEqual(g.decisions[0]!.body, {
    decision: "approve",
    code: "4821",
    channel: "imessage",
    decided_by: "imessage:approver@icloud.com",
  });
  assert.match(m.texts()[0]!, /^Approved\. It's running now\./);
  assert.equal(m.sent[0]!.key, "mulligan-reply-m1");
});

test("NO <code> rejects", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  assert.equal(await handleReply(inbound("m1", "no 4821"), m, g), "reject");
  assert.match(m.texts()[0]!, /^Rejected\. It won't run\./);
});

test("approved but the call failed: says so", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  g.runResult = "failed";
  await handleReply(inbound("m1", "YES 4821"), m, g);
  assert.match(m.texts()[0]!, /^Approved, but it didn't run/);
});

test("unknown code, already decided, and chatter", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  assert.equal(await handleReply(inbound("m1", "YES 9999"), m, g), "no-match");
  assert.match(m.texts()[0]!, /No request is waiting with code 9999/);

  g.failWith = new GatewayError(409, "action is approved, not held");
  assert.equal(await handleReply(inbound("m2", "YES 4821"), m, g), "already-decided");

  assert.equal(await handleReply(inbound("m3", "yes please"), m, g), "unparseable");
  assert.match(m.texts().at(-1)!, /Reply "YES 1234"/);
  const before = m.sent.length;
  assert.equal(await handleReply(inbound("m4", "thanks!"), m, g), "ignored");
  assert.equal(m.sent.length, before);
});

test("gateway errors other than 404/409 still answer the approver, then propagate", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  g.failWith = new GatewayError(500, "boom");
  await assert.rejects(handleReply(inbound("m1", "YES 4821"), m, g), GatewayError);
  assert.match(m.texts()[0]!, /^Couldn't record that/);
});

test("loop: handles each new reply once and ignores ones from before startup", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  m.inbox = [inbound("old", "YES 4821", "2026-10-18T00:59:59.000Z"), inbound("new", "YES 4821")];
  const stop = new AbortController();
  const logs: string[] = [];
  const loop = replyLoop(m, g, {
    intervalMs: 5,
    signal: stop.signal,
    log: (l) => logs.push(l),
    now: () => new Date("2026-10-18T01:00:00.000Z"),
  });
  await new Promise((r) => setTimeout(r, 40));
  stop.abort();
  await loop;
  assert.equal(g.decisions.length, 1);
  assert.equal(logs.length, 1);
  assert.match(logs[0]!, /: approve$/);
});

test("loop: a failed decision goes to onError (once while it keeps failing)", async () => {
  const m = new FakeMessenger();
  const g = new FakeGateway([heldAction("4821")]);
  g.failWith = new GatewayError(500, "boom");
  m.inbox = [inbound("a", "YES 4821"), inbound("b", "YES 4821")];
  const stop = new AbortController();
  const reported: unknown[] = [];
  const loop = replyLoop(m, g, {
    intervalMs: 5,
    signal: stop.signal,
    log: () => {},
    onError: (e) => reported.push(e),
    now: () => new Date("2026-10-18T01:00:00.000Z"),
  });
  await new Promise((r) => setTimeout(r, 40));
  stop.abort();
  await loop;
  assert.equal(reported.length, 1);
  assert.ok(reported[0] instanceof GatewayError);
});
