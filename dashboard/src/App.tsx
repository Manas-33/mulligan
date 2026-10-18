// The Mulligan dashboard: one agent session ("round") at a time. The round screen shows what the
// agent did and what's held; the rewind screens show the plan, the live run and the proof.

import { lazy, Suspense, useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { createClient } from "./api/client.ts";
import { streamEvents } from "./api/sse.ts";
import { Course } from "./components/Course.tsx";
import { Hero } from "./components/Hero.tsx";
import { Holds } from "./components/Holds.tsx";
import { Rewind } from "./components/Rewind.tsx";
import { Logo } from "./components/SystemObject.tsx";
import { API_URL, DASHBOARD_TOKEN } from "./config.ts";
import type { StreamStatus } from "./api/sse.ts";
import { GatewayProblem, Loading, NoSessions } from "./components/States.tsx";
import { empty, heldActions, latestRewind, reduce, sessionList, timeline } from "./state.ts";
import type { Action, RewindJob } from "./types.ts";
import { describeProblem, isUndoable, type Problem, shortId } from "./view.ts";

// The sponsor-evidence page is its own chunk: judges open it, the demo doesn't need it.
const Evidence = lazy(() => import("./components/Evidence.tsx"));

/** "#/evidence" shows the evidence page; anything else is the dashboard. */
function useRoute(): "evidence" | "dashboard" {
  const read = (): "evidence" | "dashboard" => (location.hash.startsWith("#/evidence") ? "evidence" : "dashboard");
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const on = () => {
      setRoute(read());
      scrollTo(0, 0);
    };
    addEventListener("hashchange", on);
    return () => removeEventListener("hashchange", on);
  }, []);
  return route;
}

const client = createClient({ baseUrl: API_URL, token: DASHBOARD_TOKEN });

type Conn = "connecting" | StreamStatus;

/** The REST snapshot: still coming, in, or failed (and why). */
type Load = { state: "loading" } | { state: "ready" } | { state: "failed"; problem: Problem };

/** What an undo covers: the whole session, or back to (and including) one action. */
type Target = { from?: Action };

const clock = (t: number) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });

const LOST_AFTER_MS = 4000;

const CONN_TEXT: Partial<Record<Conn, string>> = {
  connecting: "connecting…",
  reconnecting: "reconnecting…",
  unauthorized: "token refused",
};

export function App() {
  if (import.meta.env.DEV && location.hash === "#crash") throw new Error("crash test (#crash, dev only)");
  const route = useRoute();
  const [state, dispatch] = useReducer(reduce, empty);
  const [conn, setConn] = useState<Conn>("connecting");
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const loadRef = useRef(load);
  loadRef.current = load;
  const [retrying, setRetrying] = useState(false);
  const [lostAt, setLostAt] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [rewinding, setRewinding] = useState<Target | null>(null);

  const fetchSnapshot = useCallback(async () => {
    setRetrying(true);
    try {
      // The newest actions: without order and limit the gateway answers with the oldest 200.
      const [sessions, actions] = await Promise.all([
        client.listSessions(),
        client.listActions({ order: "desc", limit: "1000" }),
      ]);
      dispatch({ type: "snapshot", sessions, actions });
      setLoad({ state: "ready" });
    } catch (e) {
      // Keep what's on screen if a refresh fails; only an empty page shows the full problem.
      setLoad((l) => (l.state === "ready" ? l : { state: "failed", problem: describeProblem(e) }));
    } finally {
      setRetrying(false);
    }
  }, []);

  useEffect(() => {
    const stop = new AbortController();
    let connected = false;
    void fetchSnapshot();
    void streamEvents({
      baseUrl: API_URL,
      token: DASHBOARD_TOKEN,
      signal: stop.signal,
      onStatus: (s) => {
        setConn(s);
        if (s === "live") {
          setLostAt(null);
          // Back after a drop, or the gateway came up after the dashboard: catch up on anything missed.
          if (connected || loadRef.current.state === "failed") void fetchSnapshot();
          connected = true;
        } else if (s === "reconnecting") {
          setLostAt((t) => t ?? Date.now());
        }
      },
      onEvent: (event) => dispatch({ type: "event", event }),
    });
    return () => stop.abort();
  }, [fetchSnapshot]);

  // A blip (a gateway restart, Prism closing each mock stream) isn't news; a drop that lasts is.
  const [feedLost, setFeedLost] = useState(false);
  useEffect(() => {
    setFeedLost(false);
    if (lostAt === null) return;
    const t = setTimeout(() => setFeedLost(true), Math.max(0, lostAt + LOST_AFTER_MS - Date.now()));
    return () => clearTimeout(t);
  }, [lostAt]);

  // While nothing answers, keep looking: the gateway often starts after the dashboard.
  useEffect(() => {
    if (load.state !== "failed" || load.problem.kind !== "unreachable") return;
    const t = setInterval(() => void fetchSnapshot(), 5000);
    return () => clearInterval(t);
  }, [load, fetchSnapshot]);

  const sessions = useMemo(() => sessionList(state), [state]);
  const current = selected ?? sessions[0]?.id ?? null;
  const session = current ? state.sessions[current] : undefined;
  const actions = useMemo(() => (current ? timeline(state, current) : []), [state, current]);
  const held = useMemo(() => heldActions(state), [state]);
  const canRewind = actions.some(isUndoable);
  const onJob = useCallback((job: RewindJob) => dispatch({ type: "rewind", job }), []);

  return (
    <div className="app">
      <nav className="nav" aria-label="Mulligan">
        <a className="brand" href="#/">
          <Logo /> Mulligan
        </a>
        <span className="tagline">Records what your AI agent does, and undoes it.</span>
        {route === "dashboard" && sessions.length > 1 && (
          <label className="round-picker">
            <span className="quiet">session</span>
            <select
              value={current ?? ""}
              onChange={(e) => {
                setSelected(e.target.value);
                setRewinding(null);
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
          {CONN_TEXT[conn] ?? `watching · ${state.lastSeq} events`}
        </span>
      </nav>

      {route === "evidence" ? (
        <Suspense fallback={<Loading />}>
          <Evidence />
        </Suspense>
      ) : (
        <>
          {load.state === "ready" && conn === "unauthorized" && (
            <p role="alert" className="notice notice-bad">
              The live feed refused the dashboard’s token, so nothing new will show up. Check <code className="mono">MULLIGAN_DASHBOARD_TOKEN</code>.
            </p>
          )}
          {load.state === "ready" && feedLost && lostAt !== null && (
            <p role="status" className="notice">
              Lost the live feed at <span className="mono">{clock(lostAt)}</span>. What’s here may be out of date; reconnecting every
              couple of seconds.
            </p>
          )}

          {load.state === "loading" ? (
            <Loading />
          ) : load.state === "failed" && !session ? (
            <GatewayProblem problem={load.problem} apiUrl={API_URL} onRetry={() => void fetchSnapshot()} retrying={retrying} />
          ) : !session ? (
            <NoSessions />
          ) : rewinding ? (
            <Rewind
              client={client}
              session={session}
              actions={state.actions}
              from={rewinding.from}
              live={latestRewind(state, session.id)}
              onJob={onJob}
              onClose={() => setRewinding(null)}
            />
          ) : (
            <>
              <Hero session={session} actions={actions} canRewind={canRewind} onRewind={() => setRewinding({})} />
              <div className="body">
                <Course session={session} actions={actions} onRewindTo={(from) => setRewinding({ from })} />
                <Holds client={client} held={held} />
              </div>
            </>
          )}
          <footer className="foot">
            <a className="link-button quiet" href="#/evidence">
              How Mulligan was built →
            </a>
          </footer>
        </>
      )}
    </div>
  );
}
