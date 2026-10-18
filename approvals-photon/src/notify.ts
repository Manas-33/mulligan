// POST /notify/approval and /notify/summary (contracts/photon.openapi.yaml).

import { NotReachable, type Messenger } from "./photon.ts";
import type { NotifyApprovalRequest, NotifyResponse, NotifySummaryRequest } from "./types.ts";

// Same patterns as contracts/action.schema.json.
const ACTION_ID = /^act_[0-9A-HJKMNP-TV-Z]{26}$/;
const REWIND_ID = /^rwd_[0-9A-HJKMNP-TV-Z]{26}$/;
const CODE = /^[0-9]{4}$/;
const HANDLE = /^(\+[1-9][0-9]{6,14}|[^@\s]+@[^@\s]+\.[^@\s]+)$/;

export type Reply = { status: number; body: unknown };

const invalid = (detail: string): Reply => ({ status: 422, body: { detail } });
const notReachable: Reply = { status: 409, body: { detail: "approver_not_reachable" } };

const str = (v: unknown): v is string => typeof v === "string";

export function validateApproval(b: unknown): NotifyApprovalRequest | string {
  const r = (b ?? {}) as Record<string, unknown>;
  if (!str(r.action_id) || !ACTION_ID.test(r.action_id)) return "action_id must be act_<ulid>";
  if (!str(r.code) || !CODE.test(r.code)) return "code must be 4 digits";
  if (!str(r.summary) || !r.summary.trim()) return "summary is required";
  if (!str(r.to) || !HANDLE.test(r.to)) return "to must be an E.164 phone number or an email";
  return { action_id: r.action_id, code: r.code, summary: r.summary.slice(0, 300), to: r.to };
}

export function validateSummary(b: unknown): NotifySummaryRequest | string {
  const r = (b ?? {}) as Record<string, unknown>;
  if (!str(r.rewind_job_id) || !REWIND_ID.test(r.rewind_job_id)) return "rewind_job_id must be rwd_<ulid>";
  if (!str(r.text) || !r.text.trim()) return "text is required";
  if (!str(r.to) || !HANDLE.test(r.to)) return "to must be an E.164 phone number or an email";
  return { rewind_job_id: r.rewind_job_id, text: r.text, to: r.to };
}

export function approvalText(req: NotifyApprovalRequest): string {
  return `Mulligan: ${req.summary}\nReply YES ${req.code} to approve or NO ${req.code} to reject.`;
}

async function deliver(messenger: Messenger, to: string, text: string, key: string): Promise<Reply> {
  const convo = await messenger.findConversation(to);
  if (!convo) return notReachable;
  try {
    const id = await messenger.send(convo, text, key);
    return { status: 202, body: { message_id: id, status: "queued" } satisfies NotifyResponse };
  } catch (e) {
    if (e instanceof NotReachable) return notReachable;
    throw e;
  }
}

export async function notifyApproval(messenger: Messenger, body: unknown): Promise<Reply> {
  const req = validateApproval(body);
  if (typeof req === "string") return invalid(req);
  // Keyed on action and code, so a gateway retry never texts twice.
  return deliver(messenger, req.to, approvalText(req), `mulligan-approval-${req.action_id}-${req.code}`);
}

export async function notifySummary(messenger: Messenger, body: unknown): Promise<Reply> {
  const req = validateSummary(body);
  if (typeof req === "string") return invalid(req);
  return deliver(messenger, req.to, `Mulligan: ${req.text}`, `mulligan-summary-${req.rewind_job_id}`);
}
