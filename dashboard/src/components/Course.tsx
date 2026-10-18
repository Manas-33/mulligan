// "What the agent did": one lane per system, a bead per action placed by time, held actions as red
// flags, reads as small faded dots, a legend for all three, and the selected action underneath.

import { useState } from "react";
import { SENTRY_TRACE_URL } from "../config.ts";
import type { Action, Session, System } from "../types.ts";
import { isRead, labelText, laneCaption, position, shortId, SYSTEM_ORDER, systemName, undoableFrom } from "../view.ts";
import { DitherField } from "./Dither.tsx";
import { Flag, RewindGlyph, SystemObject } from "./SystemObject.tsx";

const clock = (iso: string) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

type CourseProps = { session: Session; actions: Action[]; onRewindTo: (action: Action) => void };

export function Course({ session, actions, onRewindTo }: CourseProps) {
  const [picked, setPicked] = useState<string | null>(null);
  const start = session.started_at;
  const end = session.ended_at ?? actions[0]?.created_at ?? start;
  const systems = SYSTEM_ORDER.filter((s) => actions.some((a) => a.system === s));
  const selected =
    actions.find((a) => a.id === picked) ?? actions.find((a) => a.status !== "held" && !isRead(a)) ?? actions.find((a) => a.status !== "held") ?? actions[0];

  return (
    <section className="course card" aria-labelledby="course-title">
      <DitherField className="course-dither" />
      <header className="section-head">
        <h2 id="course-title">What the agent did</h2>
        <span className="mono quiet">
          {clock(start)} → {session.ended_at ? clock(end) : "now"}
        </span>
      </header>
      {systems.length === 0 ? (
        <p className="quiet empty">Nothing yet. Actions show up here as the agent makes them.</p>
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
      {systems.length > 0 && <Legend />}
      {selected && (
        <Selected action={selected} undoes={undoableFrom(actions, selected.created_at).length} onRewindTo={() => onRewindTo(selected)} />
      )}
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
          const read = isRead(a);
          return (
            <button
              key={a.id}
              type="button"
              className={`bead bead-${system} status-${a.status} ${read ? "is-read" : ""} ${a.id === selectedId ? "is-selected" : ""}`}
              style={{ left: `${position(a.created_at, start, end) * 100}%` }}
              aria-label={`${a.summary ?? a.tool}, ${read ? "read" : a.status}`}
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

function Legend() {
  return (
    <ul className="legend quiet small" aria-label="Key">
      <li>
        <span className="bead bead-postgres is-read" aria-hidden /> read, nothing to undo
      </li>
      <li>
        <span className="bead bead-postgres" aria-hidden /> change, can be undone
      </li>
      <li>
        <Flag size={14} /> waiting for your approval
      </li>
    </ul>
  );
}

function Selected({ action, undoes, onRewindTo }: { action: Action; undoes: number; onRewindTo: () => void }) {
  const time = new Date(action.created_at).toLocaleTimeString([], { hour12: false });
  const read = isRead(action);
  const why = read ? "A read. It changed nothing, so there’s nothing to undo." : (action.explanation ?? action.reversibility.reason);
  return (
    <div className="selected" aria-live="polite">
      <SystemObject system={action.system} size={52} />
      <div className="selected-body">
        <p className="mono quiet">
          #{shortId(action.id)} · {time} · {labelText(action.reversibility.label)}
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
      {!read && (
        <div className="rewind-to">
          <button className="pill-button small" disabled={undoes === 0} onClick={onRewindTo}>
            <RewindGlyph size={16} /> Undo from here
          </button>
          <span className="quiet small">
            {undoes === 0 ? "Nothing to undo from here." : undoes === 1 ? "Undoes just this one." : `Undoes this and the ${undoes - 1} after it.`}
          </span>
        </div>
      )}
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
      {captured ? ", so undoing it now refunds it." : " unless you undo it first."}
    </p>
  );
}
