import assert from "node:assert/strict";
import type { AddressInfo } from "node:net";
import { after, before, test } from "node:test";
import { createApp, tokenMatches } from "../src/app.ts";

const TOKEN = "test-service-token";
const server = createApp({ serviceToken: TOKEN });
let base = "";

before(async () => {
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});
after(() => new Promise<void>((resolve) => server.close(() => resolve())));

const post = (path: string, token?: string) =>
  fetch(base + path, {
    method: "POST",
    headers: { "content-type": "application/json", ...(token ? { "x-mulligan-service-token": token } : {}) },
    body: JSON.stringify({}),
  });

test("healthz needs no token", async () => {
  const res = await fetch(`${base}/healthz`);
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true });
});

test("notify routes reject a missing or wrong token", async () => {
  for (const token of [undefined, "wrong", `${TOKEN}x`]) {
    const res = await post("/notify/approval", token);
    assert.equal(res.status, 401, String(token));
    assert.deepEqual(await res.json(), { detail: "missing or invalid token" });
  }
});

test("notify routes are stubbed until Phase 2", async () => {
  for (const path of ["/notify/approval", "/notify/summary"]) {
    assert.equal((await post(path, TOKEN)).status, 501, path);
  }
});

test("unknown routes are 404 after auth", async () => {
  assert.equal((await post("/nope", TOKEN)).status, 404);
});

test("token comparison", () => {
  assert.ok(tokenMatches(TOKEN, TOKEN));
  assert.ok(!tokenMatches(undefined, TOKEN));
  assert.ok(!tokenMatches(["a", "b"], TOKEN));
  assert.ok(!tokenMatches("short", TOKEN));
});
