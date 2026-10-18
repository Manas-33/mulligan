// Phase 2 dashboard: sessions, a live action timeline, and approve/reject for held actions.
// Layout and styling are placeholders until the Paper design lands.

import { useEffect, useMemo, useReducer, useState } from "react";
import { ApiError, createClient } from "./api/client.ts";
import { streamEvents } from "./api/sse.ts";
import { RewindDialog } from "./components/RewindDialog.tsx";
import { API_URL, DASHBOARD_TOKEN, SENTRY_TRACE_URL } from "./config.ts";
import { empty, heldActions, latestRewind, reduce, sessionList, timeline } from "./state.ts";
import type { Action, ApprovalDecision, Session } from "./types.ts";

const client = createClient({ baseUrl: API_URL, token: DASHBOARD_TOKEN });

type Conn = "connecting" | "live" | "reconnecting" | "error";

export function App() {
  const [state, dispatch] = useReducer(reduce, empty);
  const [conn, setConn] = useState<Conn>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [rewinding, setRewinding] = useState(false);

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
  const session = current ? state.sessions[current] : undefined;
  const undoable = actions.some((a) => ["executed", "held", "approved", "conflict"].includes(a.status));

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
            <SessionItem
              key={s.id}
              session={s}
              count={timeline(state, s.id).length}
              active={s.id === current}
              onSelect={() => setSelected(s.id)}
            />
          ))}
        </nav>
        <main className="timeline">
          {held.length > 0 && (
            <section className="held-panel" aria-label="Waiting for approval">
              <h2>Waiting for approval</h2>
              <ol>
                {held.map((a) => (
                  <ActionRow key={a.id} action={a} />
                ))}
              </ol>
            </section>
          )}
          {session && (
            <div className="timeline-head">
              <h2>{session.agent.display_name}</h2>
              <span className="muted">{session.status}</span>
              <button className="danger" disabled={!undoable} onClick={() => setRewinding(true)}>
                Rewind session
              </button>
            </div>
          )}
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
      {rewinding && session && (
        <RewindDialog
          client={client}
          session={session}
          live={latestRewind(state, session.id)}
          onJob={(job) => dispatch({ type: "rewind", job })}
          onClose={() => setRewinding(false)}
        />
      )}
    </div>
  );
}

// `count` comes from live state: Session.action_count is only as fresh as the last session event.
function SessionItem(props: { session: Session; count: number; active: boolean; onSelect: () => void }) {
  const { session, count, active, onSelect } = props;
  return (
    <button className={`session ${active ? "active" : ""}`} onClick={onSelect} aria-current={active}>
      <strong>{session.agent.display_name}</strong>
      <span className="muted">
        {count} {count === 1 ? "action" : "actions"} · {session.status}
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
        {action.hold?.kind === "payment_auth" && action.hold.release_at && action.status === "executed" && (
          <PaymentHold releaseAt={action.hold.release_at} />
        )}
        {action.error && <p className="error">{action.error}</p>}
        {err && <p className="error">{err}</p>}
      </div>
      <div className="action-meta">
        <time className="muted">{new Date(action.created_at).toLocaleTimeString()}</time>
        {action.sentry_trace_id && (
          <a className="muted" href={SENTRY_TRACE_URL + action.sentry_trace_id} target="_blank" rel="noreferrer">
            trace
          </a>
        )}
      </div>
    </li>
  );
}

function PaymentHold({ releaseAt }: { releaseAt: string }) {
  const at = new Date(releaseAt);
  const captured = at.getTime() <= Date.now();
  return (
    <p className="muted">
      {captured ? "Captured at " : "Authorized only. Captures at "}
      {at.toLocaleTimeString()}
      {captured ? " (a rewind now refunds it)." : " unless rewound first."}
    </p>
  );
}
