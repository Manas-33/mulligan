import { describe, expect, it, vi } from "vitest";
import { ApiError, createClient } from "../src/api/client.ts";

function fakeFetch(status: number, body: unknown) {
  return vi.fn<typeof fetch>(async () => new Response(JSON.stringify(body), { status }));
}

describe("api client", () => {
  it("sends the bearer token and drops undefined query params", async () => {
    const f = fakeFetch(200, []);
    const client = createClient({ baseUrl: "http://127.0.0.1:4010", token: "tok", fetch: f });
    await client.listActions({ session_id: "ses_1", status: undefined });
    const [url, init] = f.mock.calls[0]!;
    expect(String(url)).toBe("http://127.0.0.1:4010/api/actions?session_id=ses_1");
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer tok");
  });

  it("raises ApiError with the gateway's detail", async () => {
    const client = createClient({ baseUrl: "http://x", token: "bad", fetch: fakeFetch(401, { detail: "missing or invalid token" }) });
    await expect(client.listSessions()).rejects.toMatchObject({ status: 401, detail: "missing or invalid token" });
    await expect(client.listSessions()).rejects.toBeInstanceOf(ApiError);
  });

  it("encodes ids in paths", async () => {
    const f = fakeFetch(200, {});
    await createClient({ baseUrl: "http://x", token: "t", fetch: f }).getAction("act/1");
    expect(String(f.mock.calls[0]![0])).toBe("http://x/api/actions/act%2F1");
  });
});

describe("decide", () => {
  it("posts the decision with the action's code from the dashboard", async () => {
    const f = fakeFetch(200, {});
    const action = { id: "act_1", approval: { code: "4821" } } as Parameters<ReturnType<typeof createClient>["decide"]>[0];
    await createClient({ baseUrl: "http://x", token: "t", fetch: f }).decide(action, "reject");
    const [url, init] = f.mock.calls[0]!;
    expect(String(url)).toBe("http://x/api/approvals/act_1/decision");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ decision: "reject", code: "4821", channel: "dashboard", decided_by: "dashboard" });
  });
});

describe("rewind", () => {
  it("posts the scope with dry_run", async () => {
    const f = fakeFetch(200, { id: "rwd_1" });
    await createClient({ baseUrl: "http://x", token: "t", fetch: f }).rewind({ session_id: "ses_1" }, true);
    const [url, init] = f.mock.calls[0]!;
    expect(String(url)).toBe("http://x/api/rewind");
    expect(JSON.parse(String(init?.body))).toEqual({ scope: { session_id: "ses_1" }, dry_run: true, requested_by: "dashboard" });
  });
});
