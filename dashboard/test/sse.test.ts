import { describe, expect, it, vi } from "vitest";
import { createFrameParser, streamEvents } from "../src/api/sse.ts";
import type { MulliganEvent } from "../src/types.ts";

describe("SSE frame parser", () => {
  it("parses frames split across chunks and skips pings", () => {
    const parse = createFrameParser();
    expect(parse(": ping\n\nid: 41\nevent: action.held\nda")).toEqual([]);
    expect(parse('ta: {"seq":41}\n\nid: 42\r\ndata: {"seq":42}\r\n\r\n')).toEqual([
      { id: "41", event: "action.held", data: '{"seq":41}' },
      { id: "42", data: '{"seq":42}' },
    ]);
  });

  it("joins multi-line data", () => {
    expect(createFrameParser()("data: a\ndata: b\n\n")).toEqual([{ data: "a\nb" }]);
  });
});

const frame = (seq: number) =>
  `id: ${seq}\nevent: action.held\ndata: ${JSON.stringify({ event: "action.held", seq, ts: "t", data: {} })}\n\n`;

function streamResponse(text: string, status = 200) {
  return new Response(
    new ReadableStream({
      start(c) {
        c.enqueue(new TextEncoder().encode(text));
        c.close();
      },
    }),
    { status },
  );
}

describe("streamEvents", () => {
  it("sends the token, delivers events, and resumes with Last-Event-ID", async () => {
    const stop = new AbortController();
    const seen: number[] = [];
    const calls: Headers[] = [];
    const f = vi.fn<typeof fetch>(async (_url, init) => {
      calls.push(new Headers(init?.headers));
      if (calls.length === 1) return streamResponse(frame(1) + frame(2));
      if (calls.length === 2) return streamResponse(frame(3));
      stop.abort();
      throw new DOMException("aborted", "AbortError");
    });
    await streamEvents({
      baseUrl: "http://x",
      token: "tok",
      signal: stop.signal,
      fetch: f,
      retryMs: 1,
      onEvent: (e: MulliganEvent) => seen.push(e.seq),
    });
    expect(seen).toEqual([1, 2, 3]);
    expect(calls[0]!.get("authorization")).toBe("Bearer tok");
    expect(calls[0]!.get("last-event-id")).toBeNull();
    expect(calls[1]!.get("last-event-id")).toBe("2");
    expect(calls[2]!.get("last-event-id")).toBe("3");
  });

  it("reports reconnecting on HTTP errors", async () => {
    const stop = new AbortController();
    const statuses: string[] = [];
    const f = vi.fn<typeof fetch>(async () => {
      if (statuses.length >= 2) stop.abort();
      return streamResponse("", 503);
    });
    await streamEvents({ baseUrl: "http://x", token: "t", signal: stop.signal, fetch: f, retryMs: 1, onEvent: () => {}, onStatus: (s) => statuses.push(s) });
    expect(statuses.every((s) => s === "reconnecting")).toBe(true);
  });
});

describe("streamEvents on a refused token", () => {
  it("reports unauthorized once and stops retrying", async () => {
    const statuses: string[] = [];
    const f = vi.fn<typeof fetch>(async () => new Response("", { status: 401 }));
    await streamEvents({
      baseUrl: "http://x",
      token: "bad",
      signal: new AbortController().signal,
      fetch: f,
      retryMs: 1,
      onEvent: () => {},
      onStatus: (s) => statuses.push(s),
    });
    expect(f).toHaveBeenCalledTimes(1);
    expect(statuses).toEqual(["unauthorized"]);
  });
});
