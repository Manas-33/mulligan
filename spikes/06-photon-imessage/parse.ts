// Parses an approver's iMessage reply like "YES 4821" or "no 4821".
// Kept separate from spike.ts so the rules can be tested without a phone.

export type Decision = { decision: "approve" | "reject"; code: string };

const REPLY = /^\s*(yes|y|approve|no|n|reject)[\s,.:!-]*(\d{4})\s*[.!]?\s*$/i;

export function parseReply(text: string): Decision | null {
  const m = REPLY.exec(text);
  if (!m) return null;
  const word = m[1].toLowerCase();
  return { decision: ["yes", "y", "approve"].includes(word) ? "approve" : "reject", code: m[2] };
}

// An approver handle is an E.164 phone or an iMessage email (Apple ID), as Photon reports it.
// Phones compare by digits ("+1 (213) 855-7431" == "+12138557431"); emails case-insensitively.
export function sameHandle(a: string | undefined, b: string): boolean {
  if (!a) return false;
  if (a.includes("@") || b.includes("@")) return a.trim().toLowerCase() === b.trim().toLowerCase();
  const digits = (s: string) => s.replace(/\D/g, "");
  return digits(a) !== "" && digits(a) === digits(b);
}
