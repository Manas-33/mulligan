// The slice of Photon 2.0 (@photon-ai/api) the service needs, behind an interface so tests can fake it.
// Shared-line rules (spike 0.6): the line can't text first, so we only send into a conversation the
// approver started; threaded replies are refused, so every send is a plain message.

import { ApiError, Photon } from "@photon-ai/api";
import { sameHandle } from "./parse.ts";

export type Conversation = { spaceId: string; senderId: string };

export type InboundText = {
  id: string;
  createdAt: string;
  from: string;
  /** Where to answer: the same direct conversation, sending as the line that received it. */
  conversation: Conversation;
  text: string;
};

export interface Messenger {
  /** The approver's direct conversation with the line, or null if they never texted it. */
  findConversation(handle: string): Promise<Conversation | null>;
  /** Send a plain text. Returns Photon's message id. Same idempotency key = same message. */
  send(to: Conversation, text: string, idempotencyKey: string): Promise<string>;
  /** Text messages received at or after `since` (RFC 3339), oldest first. */
  receivedSince(since: string): Promise<InboundText[]>;
}

/** Thrown by send() when Photon refuses because the approver hasn't texted the line. */
export class NotReachable extends Error {}

// The client types message items loosely; these are the fields we read.
type WireMessage = {
  id: string;
  createdAt: string;
  sentBy?: { handle?: string };
  receivedBy?: { id?: string };
  space?: { id?: string; kind?: string };
  content?: { type?: string; text?: string };
};

export function photonMessenger(projectId: string, apiKey: string): Messenger {
  const photon = new Photon({ headers: { Authorization: `Bearer ${apiKey}` } });
  const path = { id: projectId };
  const cache = new Map<string, Conversation>();

  const list = async (query: Record<string, string | number>) =>
    (await photon.projects.listMessages({ path, query } as never)).items as unknown as WireMessage[];

  return {
    async findConversation(handle) {
      const cached = [...cache].find(([h]) => sameHandle(h, handle))?.[1];
      if (cached) return cached;
      const recent = await list({ direction: "received", platform: "imessage", order: "desc", pageSize: 100 });
      const m = recent.find((r) => sameHandle(r.sentBy?.handle, handle) && r.space?.kind === "direct");
      if (!m?.space?.id || !m.receivedBy?.id) return null;
      const convo = { spaceId: m.space.id, senderId: m.receivedBy.id };
      cache.set(handle, convo);
      return convo;
    },

    async send(to, text, idempotencyKey) {
      try {
        const sent = await photon.projects.sendMessage({
          path,
          headers: { "idempotency-key": idempotencyKey },
          body: { spaceId: to.spaceId, senderId: to.senderId, content: { type: "text", format: "plain", text } },
        });
        return String((sent as { id?: string }).id ?? idempotencyKey);
      } catch (e) {
        const code = e instanceof ApiError ? (e.body as { code?: string } | undefined)?.code : undefined;
        if (code === "SHARED_LINE_INBOUND_REQUIRED") throw new NotReachable(code);
        throw e;
      }
    },

    async receivedSince(since) {
      const items = await list({ direction: "received", platform: "imessage", order: "asc", pageSize: 100, createdSince: since });
      return items
        // Direct chats only: a group the line is in isn't where approvals happen.
        .filter(
          (m) =>
            m.content?.type === "text" &&
            m.space?.kind === "direct" &&
            m.sentBy?.handle &&
            m.space?.id &&
            m.receivedBy?.id,
        )
        .map((m) => ({
          id: m.id,
          createdAt: m.createdAt,
          from: m.sentBy!.handle!,
          conversation: { spaceId: m.space!.id!, senderId: m.receivedBy!.id! },
          text: m.content!.text ?? "",
        }));
    },
  };
}
