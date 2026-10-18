// Phase 2 dashboard: sessions, a live action timeline, and approve/reject for held actions.
// Layout and styling are placeholders until the Paper design lands.

import { useEffect, useMemo, useReducer, useState } from "react";
import { ApiError, createClient } from "./api/client.ts";
import { streamEvents } from "./api/sse.ts";
import { API_URL, DASHBOARD_TOKEN } from "./config.ts";
import { empty, heldActions, reduce, sessionList, timeline } from "./state.ts";
import type { Action, ApprovalDecision, Session } from "./types.ts";

const client = createClient({ baseUrl: API_URL, token: DASHBOARD_TOKEN });

type Conn = "connecting" | "live" | "reconnecting" | "error";

export function App() {
  const [state, dispatch] = useReducer(reduce, empty);
  const [conn, setConn] = useState<Conn>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    const stop = new AbortController();
    Promise.all([client.listSessions(), client.listActions()])
      .then(([sessions, actions]) => dispatch({ type: "snapshot", sessions, actions }))
      .catch((e: unknown) => {
        setConn("error");
        setError(e instanceof Error ? e.message : String(e));
      });
    void streamEvents({
      baseUrl: API_URL,
      token: DASHBOARD_TOKEN,
      signal: stop.signal,
      onStatus: setConn,
      onEvent: (event) => dispatch({ type: "event", event }),
    });
    return () => stop.abort();
  }, []);

  const sessions = useMemo(() => sessionList(state), [state]);
  const current = selected ?? sessions[0]?.id ?? null;
  const actions = useMemo(() => (current ? timeline(state, current) : []), [state, current]);
  const held = useMemo(() => heldActions(state), [state]);

  return (
    <div className="app">
      <header className="topbar">
        <h1>Mulligan</h1>
        <span className={`conn conn-${conn}`}>{conn}</span>
        <span className="muted">{API_URL}</span>
        {held.length > 0 && <span className="pill pill-held">{held.length} waiting for approval</span>}
      </header>
      {error && <p role="alert" className="error">Can't reach the API: {error}</p>}
      <div className="columns">
        <nav className="sessions" aria-label="Sessions">
          {sessions.length === 0 && <p className="muted">No sessions yet.</p>}
          {sessions.map((s) => (
            <SessionItem key={s.id} session={s} active={s.id === current} onSelect={() => setSelected(s.id)} />
          ))}
        </nav>
        <main className="timeline">
          {actions.length === 0 ? (
            <p className="muted">No actions in this session yet.</p>
          ) : (
            <ol>
              {actions.map((a) => (
                <ActionRow key={a.id} action={a} />
              ))}
            </ol>
          )}
        </main>
      </div>
    </div>
  );
}

function SessionItem({ session, active, onSelect }: { session: Session; active: boolean; onSelect: () => void }) {
  return (
    <button className={`session ${active ? "active" : ""}`} onClick={onSelect} aria-current={active}>
      <strong>{session.agent.display_name}</strong>
      <span className="muted">
        {session.action_count} actions · {session.status}
        {session.mode === "replay" && <span className="pill">replay</span>}
      </span>
      <time className="muted">{new Date(session.started_at).toLocaleTimeString()}</time>
    </button>
  );
}

export function ActionRow({ action }: { action: Action }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const decide = async (decision: ApprovalDecision) => {
    setBusy(true);
    setErr(null);
    try {
      await client.decide(action, decision); // the SSE event updates the row
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <li className={`action status-${action.status}`}>
      <span className={`lane lane-${action.system}`}>{action.system}</span>
      <div className="action-body">
        <div className="action-head">
          <code>{action.tool}</code>
          <span className={`pill pill-${action.status}`}>{action.status}</span>
          <span className="pill" title={action.reversibility.reason ?? undefined}>
            {action.reversibility.label.replace("_", " ")}
          </span>
          {action.upstream_server && <span className="pill">not undoable</span>}
        </div>
        <p>{action.summary ?? <span className="muted">{JSON.stringify(action.args_redacted)}</span>}</p>
        {action.status === "held" && action.approval && (
          <div className="held">
            <span>
              Code <strong>{action.approval.code}</strong>
              {action.reversibility.reason && <> · {action.reversibility.reason}</>}
            </span>
            <button disabled={busy} onClick={() => decide("approve")}>
              Approve
            </button>
            <button disabled={busy} className="secondary" onClick={() => decide("reject")}>
              Reject
            </button>
          </div>
        )}
        {action.error && <p className="error">{action.error}</p>}
        {err && <p className="error">{err}</p>}
      </div>
      <time className="muted">{new Date(action.created_at).toLocaleTimeString()}</time>
    </li>
  );
}
