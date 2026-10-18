// Whole-screen states: still loading, nothing played yet, the gateway can't be reached or turns the
// token away, and the crash screen the error boundary shows. Same hero layout as a round, so the
// page doesn't jump when the real thing arrives.

import type { Problem } from "../view.ts";
import { Flag, Logo, SystemObject } from "./SystemObject.tsx";
import type { System } from "../types.ts";

const COURSE: System[] = ["postgres", "github", "notion", "email", "payments"];

/** Before the first snapshot lands. */
export function Loading() {
  return (
    <section className="hero state" aria-busy="true">
      <div className="hero-text">
        <p className="meta">connecting to the gateway</p>
        <h1 className="headline">
          <span className="line">Walking out to the course…</span>
        </h1>
      </div>
    </section>
  );
}

/** The gateway answered, and there are no sessions. */
export function NoRounds() {
  return (
    <section className="hero state" aria-labelledby="state-title">
      <div className="hero-text">
        <p className="meta">0 rounds</p>
        <h1 id="state-title" className="headline">
          <span className="line">No rounds yet.</span>
        </h1>
        <p className="lede">
          The course is set up and nobody has teed off. When an agent connects through Mulligan, its session shows up here
          and every call it makes lands on the timeline.
        </p>
      </div>
      <div className="tee-up" aria-hidden>
        {COURSE.map((s, i) => (
          <SystemObject key={s} system={s} size={i % 2 ? 74 : 88} className="tee-up-item" style={{ animationDelay: `${i * -1.1}s` }} />
        ))}
        <span className="tee-up-line" />
      </div>
    </section>
  );
}

/** The snapshot failed: unreachable, 401, or the gateway said something else. */
export function GatewayProblem({ problem, apiUrl, onRetry, retrying }: { problem: Problem; apiUrl: string; onRetry: () => void; retrying: boolean }) {
  const lost = problem.kind === "unreachable";
  return (
    <section className="hero state" aria-labelledby="state-title" role="alert">
      <div className="hero-text">
        <p className="meta">
          {apiUrl} · {problem.status ? `HTTP ${problem.status}` : "no answer"}
        </p>
        <h1 id="state-title" className="headline">
          <span className="line">{problem.kind === "unauthorized" ? "Wrong locker key." : lost ? "Can’t find the course." : "The gateway said no."}</span>
        </h1>
        <p className="lede">
          {problem.kind === "unauthorized" ? (
            <>
              The gateway turned the dashboard’s token away. Set <code className="mono">MULLIGAN_DASHBOARD_TOKEN</code> in{" "}
              <code className="mono">.env</code> to the gateway’s value, then rebuild the dashboard.
            </>
          ) : lost ? (
            <>
              Nothing answered at that address. Start the gateway (<code className="mono">make gateway</code>, or{" "}
              <code className="mono">docker compose --profile app up</code>) and this page picks it up on its own.
            </>
          ) : (
            problem.detail
          )}
        </p>
        <div className="hero-actions">
          <button className="pill-button small" onClick={onRetry} disabled={retrying}>
            {retrying ? "Looking…" : "Try again"}
          </button>
          {lost && <span className="quiet small">Also trying every few seconds.</span>}
        </div>
      </div>
      <div className="lost-flag" aria-hidden>
        <Flag size={150} className="knocked" />
        <span className="tee-up-line" />
      </div>
    </section>
  );
}

/** What Sentry.ErrorBoundary shows when rendering throws. The error itself goes to Sentry. */
export function Crashed({ eventId, onReset }: { eventId?: string; onReset: () => void }) {
  return (
    <div className="app">
      <nav className="nav" aria-label="Mulligan">
        <span className="brand">
          <Logo /> Mulligan
        </span>
      </nav>
      <section className="hero state" aria-labelledby="crash-title" role="alert">
        <div className="hero-text">
          <p className="meta">dashboard error{eventId && <> · sentry event {eventId.slice(0, 12)}</>}</p>
          <h1 id="crash-title" className="headline">
            <span className="line">Out of bounds.</span>
            <span className="line">
              Take a <span className="sticker sticker-sun">drop.</span>
            </span>
          </h1>
          <p className="lede">
            The dashboard hit a bug and stopped drawing. Nothing in your systems changed because of it, and a rewind that was
            already running keeps going on the gateway.
          </p>
          <div className="hero-actions">
            <button className="pill-button" onClick={() => location.reload()}>
              Reload the page
            </button>
            <button className="link-button" onClick={onReset}>
              Try without reloading
            </button>
          </div>
        </div>
        <div className="lost-flag" aria-hidden>
          <Flag size={150} className="knocked" />
          <span className="tee-up-line" />
        </div>
      </section>
    </div>
  );
}
