// GET /api/events (contracts/events.md). EventSource can't send headers, so this is a small
// fetch-based reader: Bearer auth, `id:` = envelope seq, resume with Last-Event-ID, `: ping` ignored.

import type { MulliganEvent } from "../types.ts";

export type SseFrame = { id?: string; event?: string; data: string };

/** Split an SSE byte stream into frames. Feed chunks; returns complete frames and keeps the rest. */
export function createFrameParser() {
  let buffer = "";
  return (chunk: string): SseFrame[] => {
    buffer += chunk.replace(/\r\n?/g, "\n");
    const frames: SseFrame[] = [];
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const frame: SseFrame = { data: "" };
      const data: string[] = [];
      for (const line of block.split("\n")) {
        if (!line || line.startsWith(":")) continue; // comment / ping
        const i = line.indexOf(":");
        const field = i === -1 ? line : line.slice(0, i);
        const value = i === -1 ? "" : line.slice(i + 1).replace(/^ /, "");
        if (field === "data") data.push(value);
        else if (field === "id") frame.id = value;
        else if (field === "event") frame.event = value;
      }
      if (data.length) frames.push({ ...frame, data: data.join("\n") });
    }
    return frames;
  };
}

export type StreamStatus = "live" | "reconnecting" | "unauthorized";

export type StreamOptions = {
  baseUrl: string;
  token: string;
  onEvent: (event: MulliganEvent) => void;
  /** live on each (re)connect, reconnecting after a drop, unauthorized when the token is refused (no retry). */
  onStatus?: (status: StreamStatus) => void;
  signal: AbortSignal;
  fetch?: typeof fetch;
  retryMs?: number;
};

/** Stream events until `signal` aborts, reconnecting (and resuming from the last seq) on errors. A 401
 * or 403 ends it: retrying with the same token can't help. */
export async function streamEvents(opts: StreamOptions): Promise<void> {
  const { baseUrl, token, onEvent, onStatus, signal, fetch: doFetch = fetch, retryMs = 2000 } = opts;
  let lastId: string | undefined;
  while (!signal.aborted) {
    try {
      const headers: Record<string, string> = { Authorization: `Bearer ${token}`, Accept: "text/event-stream" };
      if (lastId) headers["Last-Event-ID"] = lastId;
      const res = await doFetch(new URL("/api/events", baseUrl), { headers, signal });
      if (res.status === 401 || res.status === 403) {
        onStatus?.("unauthorized");
        return;
      }
      if (!res.ok || !res.body) throw new Error(`events: HTTP ${res.status}`);
      onStatus?.("live");
      const parse = createFrameParser();
      const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        for (const frame of parse(value)) {
          if (frame.id) lastId = frame.id;
          try {
            onEvent(JSON.parse(frame.data) as MulliganEvent);
          } catch {
            // a malformed frame shouldn't kill the stream
          }
        }
      }
    } catch (e) {
      if (signal.aborted) return;
      void e;
    }
    onStatus?.("reconnecting");
    await new Promise((r) => setTimeout(r, retryMs));
  }
}
