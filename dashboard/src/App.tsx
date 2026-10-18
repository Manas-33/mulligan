// The Mulligan dashboard: one agent session ("round") at a time. The round screen shows what the
// agent did and what's held; the rewind screens show the plan, the live run and the proof.

import { useCallback, useEffect, useMemo, useReducer, useState } from "react";
import { createClient } from "./api/client.ts";
import { streamEvents } from "./api/sse.ts";
import { Course } from "./components/Course.tsx";
import { Hero } from "./components/Hero.tsx";
import { Holds } from "./components/Holds.tsx";
import { Rewind } from "./components/Rewind.tsx";
import { Logo } from "./components/SystemObject.tsx";
import { API_URL, DASHBOARD_TOKEN } from "./config.ts";
import { empty, heldActions, latestRewind, reduce, sessionList, timeline } from "./state.ts";
import type { RewindJob } from "./types.ts";
import { shortId } from "./view.ts";

const client = createClient({ baseUrl: API_URL, token: DASHBOARD_TOKEN });

type Conn = "connecting" | "live" | "reconnecting" | "error";

const UNDOABLE = new Set(["executed", "held", "approved", "conflict"]);

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
  const session = current ? state.sessions[current] : undefined;
  const actions = useMemo(() => (current ? timeline(state, current) : []), [state, current]);
  const held = useMemo(() => heldActions(state), [state]);
  const canRewind = actions.some((a) => UNDOABLE.has(a.status));
  const onJob = useCallback((job: RewindJob) => dispatch({ type: "rewind", job }), []);

  return (
    <div className="app">
      <nav className="nav" aria-label="Mulligan">
        <span className="brand">
          <Logo /> Mulligan
        </span>
        {sessions.length > 1 && (
          <label className="round-picker">
            <span className="quiet">round</span>
            <select
              value={current ?? ""}
              onChange={(e) => {
                setSelected(e.target.value);
                setRewinding(false);
              }}
            >
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {shortId(s.id)} · {s.agent.display_name} · {s.status}
                </option>
              ))}
            </select>
          </label>
        )}
        <span className={`conn conn-${conn}`} role="status">
          <span className="dot" aria-hidden />
          {conn === "live" ? `watching · ${state.lastSeq} events` : conn === "connecting" ? "connecting…" : conn === "reconnecting" ? "reconnecting…" : "offline"}
        </span>
      </nav>

      {error && (
        <p role="alert" className="error banner">
          Can’t reach the gateway at {API_URL}: {error}
        </p>
      )}

      {!session ? (
        <section className="hero">
          <div className="hero-text">
            <h1 className="headline">
              <span className="line">No rounds yet.</span>
            </h1>
            <p className="lede">When an agent connects through Mulligan, its session shows up here.</p>
          </div>
        </section>
      ) : rewinding ? (
        <Rewind
          client={client}
          session={session}
          actions={state.actions}
          live={latestRewind(state, session.id)}
          onJob={onJob}
          onClose={() => setRewinding(false)}
        />
      ) : (
        <>
          <Hero session={session} actions={actions} canRewind={canRewind} onRewind={() => setRewinding(true)} />
          <div className="body">
            <Course session={session} actions={actions} />
            <Holds client={client} held={held} />
          </div>
        </>
      )}
    </div>
  );
}
