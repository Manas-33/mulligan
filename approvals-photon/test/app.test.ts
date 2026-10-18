import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { after, before, beforeEach, test } from "node:test";
import { createApp, tokenMatches } from "../src/app.ts";
import { FakeMessenger } from "./fakes.ts";

const TOKEN = "test-service-token";
const messenger = new FakeMessenger();
const server = createApp({ serviceToken: TOKEN, messenger, log: () => {} });
let base = "";

before(async () => {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => new Promise<void>((resolve) => server.close(() => resolve())));
beforeEach(() => {
  messenger.sent = [];
  messenger.refuseSend = false;
});

const APPROVAL = {
  action_id: "act_01M561R2302EME3JAF7RBZWM16",
  code: "4821",
  summary: 'Claude Code wants to run email_send: to=all-customers, subject="Price update"',
  to: "approver@icloud.com",
};

// token null = send no token header (a default parameter would swallow an explicit undefined)
const post = (path: string, body: unknown, token: string | null = TOKEN) =>
  fetch(base + path, {
    method: "POST",
    headers: { "content-type": "application/json", ...(token ? { "x-mulligan-service-token": token } : {}) },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

test("healthz needs no token", async () => {
  const res = await fetch(`${base}/healthz`);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true });
});

test("notify routes reject a missing or wrong token", async () => {
  for (const token of [null, "", "wrong", `${TOKEN}x`]) {
    const res = await post("/notify/approval", APPROVAL, token);
    assert.equal(res.status, 401, String(token));
  }
  assert.equal(messenger.sent.length, 0);
});

test("approval: texts the code and returns 202", async () => {
  const res = await post("/notify/approval", APPROVAL);
  assert.equal(res.status, 202);
  assert.deepEqual(await res.json(), { message_id: "msg_1", status: "queued" });
  const [sent] = messenger.sent;
  assert.match(sent!.text, /^Mulligan: Claude Code wants to run email_send/);
  assert.match(sent!.text, /Reply YES 4821 to approve or NO 4821 to reject\.$/);
  assert.equal(sent!.key, "mulligan-approval-act_01M561R2302EME3JAF7RBZWM16-4821");
});

test("approval: phone handles work too", async () => {
  assert.equal((await post("/notify/approval", { ...APPROVAL, to: "+15555550123" })).status, 202);
});

test("approval: 409 when the approver never texted the line", async () => {
  const res = await post("/notify/approval", { ...APPROVAL, to: "stranger@icloud.com" });
  assert.equal(res.status, 409);
  assert.deepEqual(await res.json(), { detail: "approver_not_reachable" });
});

test("approval: 409 when Photon refuses the send (SHARED_LINE_INBOUND_REQUIRED)", async () => {
  messenger.refuseSend = true;
  assert.equal((await post("/notify/approval", APPROVAL)).status, 409);
});

test("approval: 422 on invalid bodies", async () => {
  const bad = [
    { ...APPROVAL, action_id: "nope" },
    { ...APPROVAL, code: "48211" },
    { ...APPROVAL, summary: " " },
    { ...APPROVAL, to: "5555550123" },
    "not json",
  ];
  for (const body of bad) assert.equal((await post("/notify/approval", body)).status, 422, JSON.stringify(body));
});

test("summary: texts the rewind summary", async () => {
  const res = await post("/notify/summary", {
    rewind_job_id: "rwd_01M561RFRGVASABYV8XG40DDX0",
    text: "Rewound Claude Code's session: 5 actions undone. Checksums match.",
    to: "approver@icloud.com",
  });
  assert.equal(res.status, 202);
  assert.equal(messenger.sent[0]!.text, "Mulligan: Rewound Claude Code's session: 5 actions undone. Checksums match.");
});

test("413 on huge bodies, 404 on unknown routes", async () => {
  assert.equal((await post("/notify/approval", { ...APPROVAL, summary: "x".repeat(20_000) })).status, 413);
  assert.equal((await post("/nope", {})).status, 404);
});

test("token comparison", () => {
  assert.ok(tokenMatches(TOKEN, TOKEN));
  assert.ok(!tokenMatches(undefined, TOKEN));
  assert.ok(!tokenMatches(["a", "b"], TOKEN));
  assert.ok(!tokenMatches("short", TOKEN));
});
