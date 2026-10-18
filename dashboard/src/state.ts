// Dashboard state: sessions and actions keyed by id. REST loads a snapshot; SSE events replace
// whole objects (events carry the full object after the change, never a diff).

import type { Action, MulliganEvent, Session } from "./types.ts";

export type State = {
  sessions: Record<string, Session>;
  actions: Record<string, Action>;
  lastSeq: number;
};

export const empty: State = { sessions: {}, actions: {}, lastSeq: 0 };

export type Msg =
  | { type: "snapshot"; sessions: Session[]; actions: Action[] }
  | { type: "event"; event: MulliganEvent };

const byId = <T extends { id: string }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function reduce(state: State, msg: Msg): State {
  if (msg.type === "snapshot") {
    return { ...state, sessions: byId(msg.sessions), actions: byId(msg.actions) };
  }
  const e = msg.event;
  if (e.seq <= state.lastSeq) return state; // replayed after a reconnect
  const next = { ...state, lastSeq: e.seq };
  switch (e.event) {
    case "session.started":
    case "session.ended":
      return { ...next, sessions: { ...state.sessions, [e.data.id]: e.data } };
    case "rewind.step":
      return { ...next, actions: { ...state.actions, [e.data.action.id]: e.data.action } };
    case "rewind.planned":
    case "rewind.started":
    case "rewind.completed":
    case "rewind.failed":
    case "checksum.verified":
      return next; // shown by the rewind screens (Phase 3)
    default:
      return { ...next, actions: { ...state.actions, [e.data.id]: e.data } };
  }
}

/** Sessions newest first. */
export const sessionList = (s: State) => Object.values(s.sessions).sort((a, b) => b.started_at.localeCompare(a.started_at));

/** A session's actions newest first; ids are ULIDs, so they sort by creation time. */
export const timeline = (s: State, sessionId: string) =>
  Object.values(s.actions)
    .filter((a) => a.session_id === sessionId)
    .sort((a, b) => b.id.localeCompare(a.id));

export const heldActions = (s: State) => Object.values(s.actions).filter((a) => a.status === "held");
