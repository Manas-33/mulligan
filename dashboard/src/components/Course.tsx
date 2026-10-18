// "The round so far": one lane per system, a bead per action placed by time, held actions as red
// flags, and the selected action described underneath.

import { useState } from "react";
import { SENTRY_TRACE_URL } from "../config.ts";
import type { Action, Session, System } from "../types.ts";
import { laneCaption, position, shortId, SYSTEM_ORDER, systemName } from "../view.ts";
import { DitherField } from "./Dither.tsx";
import { Flag, SystemObject } from "./SystemObject.tsx";

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

export function Course({ session, actions }: { session: Session; actions: Action[] }) {
  const [picked, setPicked] = useState<string | null>(null);
  const start = session.started_at;
  const end = session.ended_at ?? actions[0]?.created_at ?? start;
  const systems = SYSTEM_ORDER.filter((s) => actions.some((a) => a.system === s));
  const selected = actions.find((a) => a.id === picked) ?? actions.find((a) => a.status !== "held") ?? actions[0];

  return (
    <section className="course card" aria-labelledby="course-title">
      <DitherField className="course-dither" />
      <header className="section-head">
        <h2 id="course-title">The round so far</h2>
        <span className="mono quiet">
          {clock(start)} → {session.ended_at ? clock(end) : "now"}
        </span>
      </header>
      {systems.length === 0 ? (
        <p className="quiet empty">No shots yet. Actions show up here as the agent makes them.</p>
      ) : (
        <div className="lanes-wrap">
        <ol className="lanes">
          {systems.map((system) => (
            <Lane
              key={system}
              system={system}
              actions={actions.filter((a) => a.system === system)}
              start={start}
              end={end}
              selectedId={selected?.id}
              onPick={setPicked}
            />
          ))}
        </ol>
        {session.status === "active" && (
          <div className="playhead" aria-hidden>
            <span className="mono">now</span>
          </div>
        )}
        </div>
      )}
      {selected && <Selected action={selected} />}
    </section>
  );
}

function Lane(props: { system: System; actions: Action[]; start: string; end: string; selectedId?: string; onPick: (id: string) => void }) {
  const { system, actions, start, end, selectedId, onPick } = props;
  return (
    <li className="lane">
      <div className="lane-name">
        <SystemObject system={system} size={30} />
        <div>
          <strong>{systemName(system)}</strong>
          <span className="mono quiet">{laneCaption(system, actions)}</span>
        </div>
      </div>
      <div className="lane-track">
        {actions.map((a) => {
          const held = a.status === "held";
          return (
            <button
              key={a.id}
              type="button"
              className={`bead bead-${system} status-${a.status} ${a.id === selectedId ? "is-selected" : ""}`}
              style={{ left: `${position(a.created_at, start, end) * 100}%` }}
              aria-label={`${a.summary ?? a.tool}, ${a.status}`}
              aria-pressed={a.id === selectedId}
              onClick={() => onPick(a.id)}
            >
              {held ? <Flag size={16} /> : null}
            </button>
          );
        })}
      </div>
    </li>
  );
}

function Selected({ action }: { action: Action }) {
  const time = new Date(action.created_at).toLocaleTimeString([], { hour12: false });
  const why = action.explanation ?? action.reversibility.reason;
  return (
    <div className="selected" aria-live="polite">
      <SystemObject system={action.system} size={52} />
      <div className="selected-body">
        <p className="mono quiet">
          #{shortId(action.id)} · {time} · {action.reversibility.label.replace("_", " ")}
          {action.upstream_server && <span className="badge badge-berry">not undoable</span>}
          {action.sentry_trace_id && (
            <a className="trace" href={SENTRY_TRACE_URL + action.sentry_trace_id} target="_blank" rel="noreferrer">
              trace
            </a>
          )}
        </p>
        <h3>{action.summary ?? action.tool}</h3>
        {why && <p className="quiet">{why}</p>}
        {action.hold?.kind === "payment_auth" && action.hold.release_at && action.status === "executed" && (
          <PaymentHold releaseAt={action.hold.release_at} />
        )}
        {action.error && <p className="error">{action.error}</p>}
      </div>
    </div>
  );
}

function PaymentHold({ releaseAt }: { releaseAt: string }) {
  const at = new Date(releaseAt);
  const captured = at.getTime() <= Date.now();
  return (
    <p className="quiet">
      {captured ? "Captured at " : "Only authorized. It captures at "}
      {at.toLocaleTimeString()}
      {captured ? ", so a rewind now refunds it." : " unless you rewind first."}
    </p>
  );
}
