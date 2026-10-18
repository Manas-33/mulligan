import { GatewayError, type Gateway } from "../src/gateway.ts";
import { NotReachable, type Conversation, type InboundText, type Messenger } from "../src/photon.ts";
import type { Action, ApprovalDecisionRequest } from "../src/types.ts";

export const CONVO: Conversation = { spaceId: "spc_1", senderId: "usr_line" };

export class FakeMessenger implements Messenger {
  sent: { to: Conversation; text: string; key: string }[] = [];
  inbox: InboundText[] = [];
  reachable = new Set<string>(["approver@icloud.com", "+15555550123"]);
  refuseSend = false;

  async findConversation(handle: string) {
    return this.reachable.has(handle) ? CONVO : null;
  }
  async send(to: Conversation, text: string, key: string) {
    if (this.refuseSend) throw new NotReachable("SHARED_LINE_INBOUND_REQUIRED");
    this.sent.push({ to, text, key });
    return `msg_${this.sent.length}`;
  }
  async receivedSince(since: string) {
    return this.inbox.filter((m) => m.createdAt >= since);
  }
  texts() {
    return this.sent.map((s) => s.text);
  }
}

export function inbound(id: string, text: string, createdAt = "2026-10-18T01:00:00.000Z"): InboundText {
  return { id, createdAt, from: "approver@icloud.com", conversation: CONVO, text };
}

export function heldAction(code: string, n = 1): Action {
  return {
    id: `act_01M561R2302EME3JAF7RBZWM${String(n).padStart(2, "0")}`,
    tool: "email_send",
    status: "held",
    error: null,
    approval: { code } as Action["approval"],
  } as Action;
}

export class FakeGateway implements Gateway {
  decisions: { actionId: string; body: ApprovalDecisionRequest }[] = [];
  failWith: GatewayError | null = null;
  runResult: Action["status"] = "executed";
  constructor(public held: Action[] = []) {}

  async heldActions() {
    return this.held;
  }
  async decide(actionId: string, body: ApprovalDecisionRequest) {
    if (this.failWith) throw this.failWith;
    this.decisions.push({ actionId, body });
    const a = this.held.find((x) => x.id === actionId)!;
    this.held = this.held.filter((x) => x.id !== actionId);
    return { ...a, status: body.decision === "approve" ? this.runResult : "rejected" } as Action;
  }
}
