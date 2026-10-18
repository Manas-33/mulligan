import assert from "node:assert/strict";
import { test } from "node:test";
import { parseReply, sameHandle } from "../src/parse.ts";

test("accepts the documented forms", () => {
  assert.deepEqual(parseReply("YES 4821"), { decision: "approve", code: "4821" });
  assert.deepEqual(parseReply("no 4821"), { decision: "reject", code: "4821" });
  assert.deepEqual(parseReply("  Yes, 0042. "), { decision: "approve", code: "0042" });
  assert.deepEqual(parseReply("y 1234"), { decision: "approve", code: "1234" });
  assert.deepEqual(parseReply("Reject:9999"), { decision: "reject", code: "9999" });
});

test("rejects anything ambiguous", () => {
  for (const text of ["YES", "4821", "yes 482", "yes 48211", "yes please 4821", "not 4821", "yesterday 4821", "YES 4821 NO 1111", ""]) {
    assert.equal(parseReply(text), null, text);
  }
});

test("compares approver handles", () => {
  assert.ok(sameHandle("+1 (213) 855-7431", "+12138557431"));
  assert.ok(!sameHandle("+12138557432", "+12138557431"));
  assert.ok(!sameHandle(undefined, "+12138557431"));
  assert.ok(sameHandle("Advait@iCloud.com", "advait@icloud.com"));
  assert.ok(!sameHandle("advait@icloud.com", "+12138557431"));
  assert.ok(!sameHandle("x", "y")); // no digits never matches
});
