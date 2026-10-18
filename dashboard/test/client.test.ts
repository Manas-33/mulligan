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
