// The top of a round: what the agent did, in one line, and the pile of things it touched. Reads
// ("reading the green") stay quiet; shots, the actions that changed something, carry the numbers.

import type { Action, Session, System } from "../types.ts";
import { countBySystem, duration, laneCaption, plural, shortId, shotCounts, SYSTEM_ORDER } from "../view.ts";
import { RewindGlyph, SystemObject } from "./SystemObject.tsx";

// Where each object sits in the pile (percent of the pile box) and how it leans.
const SLOTS = [
  { left: 6, top: 14, size: 150, tilt: -4 },
  { left: 44, top: 0, size: 160, tilt: 6 },
  { left: 76, top: 30, size: 104, tilt: 3 },
  { left: 22, top: 58, size: 140, tilt: -6 },
  { left: 58, top: 62, size: 140, tilt: 5 },
  { left: 84, top: 70, size: 90, tilt: -3 },
];

export function Hero(props: { session: Session; actions: Action[]; onRewind: () => void; canRewind: boolean }) {
  const { session, actions, onRewind, canRewind } = props;
  const counts = countBySystem(actions);
  const systems = SYSTEM_ORDER.filter((s) => counts[s]);
  const end = session.ended_at ?? actions[0]?.created_at ?? session.started_at;
  const rewound = session.status === "rewound";
  const { reads, shots, left, rewound: back } = shotCounts(actions);

  return (
    <section className="hero" aria-labelledby="hero-title">
      <div className="hero-text">
        <p className="meta">
          round {shortId(session.id)} · {session.agent.display_name}
          {session.mode === "replay" && <span className="badge">replay</span>} · {duration(session.started_at, end)}
        </p>
        <h1 id="hero-title" className="headline">
          {actions.length === 0 ? (
            <span className="line">Nothing on the card yet.</span>
          ) : (
            <>
              {reads > 0 && <span className="line reads">The agent read the green {reads === 1 ? "once" : `${reads} times`}</span>}
              <span className="line">
                {reads > 0 ? "and took" : "The agent took"} {shots === 0 ? "no shots" : plural(shots, "shot")}.
              </span>
              <span className="line">
                {shots === 0 ? (
                  "Nothing to take back."
                ) : (
                  <>
                    {rewound ? "You took the" : "Take a"} <span className="sticker sticker-sun">mulligan.</span>
                  </>
                )}
              </span>
            </>
          )}
        </h1>
        <div className="hero-actions">
          {shots > 0 && (left > 0 || back === 0) && (
            <button className="pill-button" onClick={onRewind} disabled={!canRewind}>
              <RewindGlyph /> {left > 0 ? `Rewind ${plural(left, "shot")}` : "Rewind the round"}
            </button>
          )}
          {back > 0 && (
            <p className="rewound">
              <span className="sticker sticker-mint">{plural(back, "shot")} rewound</span>
              {reads > 0 && <span className="quiet">· {plural(reads, "read")} left as they were</span>}
            </p>
          )}
          {!canRewind && back === 0 && shots > 0 && <span className="quiet">Nothing left to undo here.</span>}
        </div>
      </div>
      <div className="pile" aria-hidden>
        {systems.slice(0, SLOTS.length).map((system, i) => (
          <PileItem key={system} system={system} index={i} actions={actions.filter((a) => a.system === system)} />
        ))}
      </div>
    </section>
  );
}

function PileItem({ system, index, actions }: { system: System; index: number; actions: Action[] }) {
  const slot = SLOTS[index]!;
  const held = actions.some((a) => a.status === "held");
  return (
    <div
      className="pile-item"
      style={{ left: `${slot.left}%`, top: `${slot.top}%`, rotate: `${slot.tilt}deg`, animationDelay: `${index * -1.3}s` }}
    >
      {/* key on the count so a new action makes the object hop */}
      <SystemObject
        key={actions.length}
        system={system}
        size={slot.size}
        className="hop"
        style={{ width: `calc(${slot.size}px * var(--pile-scale))`, height: "auto" }}
      />
      <span className={`sticker sticker-small ${held ? "sticker-berry" : "sticker-cream"}`}>
        {held ? "held" : laneCaption(system, actions)}
      </span>
    </div>
  );
}
