import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { loadConfig, readDotenv } from "../src/config.ts";

test("reads .env lines, comments and quotes", () => {
  const dir = mkdtempSync(join(tmpdir(), "photon-env-"));
  const file = join(dir, ".env");
  writeFileSync(file, '# c\nA=1   # note\nB="two"\nEMPTY=\nURL=http://x:1/a#frag\n');
  assert.deepEqual(readDotenv(file), { A: "1", B: "two", EMPTY: "", URL: "http://x:1/a#frag" });
  assert.deepEqual(readDotenv(join(dir, "missing")), {});
});

test("APPROVER_HANDLE wins, empty falls back to APPROVER_PHONE; missing secrets fail fast", () => {
  const base = { MULLIGAN_SERVICE_TOKEN: "s", PHOTON_PROJECT_ID: "p", PHOTON_PROJECT_SECRET: "k" };
  assert.equal(loadConfig({ ...base, APPROVER_HANDLE: "a@icloud.com", APPROVER_PHONE: "+1555" }).approverHandle, "a@icloud.com");
  assert.equal(loadConfig({ ...base, APPROVER_HANDLE: "", APPROVER_PHONE: "+15555550123" }).approverHandle, "+15555550123");
  assert.throws(() => loadConfig({ ...base, PHOTON_PROJECT_SECRET: "" }), /PHOTON_PROJECT_SECRET is not set/);
});
