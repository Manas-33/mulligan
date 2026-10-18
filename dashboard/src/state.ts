// Dashboard state: sessions and actions keyed by id. REST loads a snapshot; SSE events replace
// whole objects (events carry the full object after the change, never a diff).

import type { Action, ChecksumVerifiedData, MulliganEvent, RewindJob, Session } from "./types.ts";

export type State = {
  sessions: Record<string, Session>;
  actions: Record<string, Action>;
  rewinds: Record<string, RewindJob>;
  /** checksum.verified events, newest last. */
  checks: ChecksumVerifiedData[];
  lastSeq: number;
};

export const empty: State = { sessions: {}, actions: {}, rewinds: {}, checks: [], lastSeq: 0 };

export type Msg =
  | { type: "snapshot"; sessions: Session[]; actions: Action[] }
  | { type: "event"; event: MulliganEvent }
  | { type: "rewind"; job: RewindJob };

const byId = <T extends { id: string }>(items: T[]) => Object.fromEntries(items.map((i) => [i.id, i]));

export function reduce(state: State, msg: Msg): State {
  if (msg.type === "snapshot") {
    return { ...state, sessions: byId(msg.sessions), actions: byId(msg.actions) };
  }
  if (msg.type === "rewind") {
    return { ...state, rewinds: { ...state.rewinds, [msg.job.id]: msg.job } };
  }
  const e = msg.event;
  if (e.seq <= state.lastSeq) return state; // replayed after a reconnect
  const next = { ...state, lastSeq: e.seq };
  switch (e.event) {
    case "session.started":
    case "session.ended":
      return { ...next, sessions: { ...state.sessions, [e.data.id]: e.data } };
    case "rewind.step": {
      const { rewind_job_id: jobId, step, action } = e.data;
      const job = state.rewinds[jobId];
      const rewinds = job
        ? {
            ...state.rewinds,
            [jobId]: {
              ...job,
              steps: job.steps.some((s) => s.action_id === step.action_id)
                ? job.steps.map((s) => (s.action_id === step.action_id ? step : s))
                : [...job.steps, step],
            },
          }
        : state.rewinds;
      return { ...next, rewinds, actions: { ...state.actions, [action.id]: action } };
    }
    case "rewind.planned":
    case "rewind.started":
    case "rewind.completed":
    case "rewind.failed":
      return { ...next, rewinds: { ...state.rewinds, [e.data.id]: e.data } };
    case "checksum.verified":
      return { ...next, checks: [...state.checks.slice(-49), e.data] };
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

/** The newest rewind job for a session (by id, which sorts by creation time). */
export const latestRewind = (s: State, sessionId: string): RewindJob | undefined =>
  Object.values(s.rewinds)
    .filter((j) => j.scope.session_id === sessionId)
    .sort((a, b) => b.id.localeCompare(a.id))[0];
