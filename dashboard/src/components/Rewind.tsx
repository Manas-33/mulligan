// Rewinding a round, as three full screens: the plan (a dry run, nothing changes), the live run
// (objects fly back to the tee as rewind.step events land), and the proof (checksums per system).

import { useEffect, useRef, useState } from "react";
import { ApiError, type Client } from "../api/client.ts";
import type { Action, FieldChange, RewindJob, RewindStep, Session, System } from "../types.ts";
import {
  fieldLabel,
  groupHash,
  planCounts,
  planSentence,
  rewindSummary,
  shortId,
  showValue,
  stepKind,
  SYSTEM_ORDER,
  systemName,
} from "../view.ts";
import { DitherField } from "./Dither.tsx";
import { RewindGlyph, SystemObject } from "./SystemObject.tsx";

type Props = {
  client: Client;
  session: Session;
  actions: Record<string, Action>;
  /** The newest job for this session from live state (events and polling). */
  live?: RewindJob;
  /** Rewind back to (and including) this action; the whole round when unset. */
  from?: Action;
  onJob: (job: RewindJob) => void;
  onClose: () => void;
};

export function Rewind({ client, session, actions, live, from, onJob, onClose }: Props) {
  const since = from?.created_at;
  const [plan, setPlan] = useState<RewindJob | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const top = useRef<HTMLDivElement>(null);

  const request = async (dryRun: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const job = await client.rewind(since ? { session_id: session.id, since } : { session_id: session.id }, dryRun);
      onJob(job);
      if (dryRun) setPlan(job);
      else setRunId(job.id);
      top.current?.scrollIntoView({ block: "start" });
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  };

  // Always start with the plan: nothing changes until someone has seen it. Once per session and
  // starting point, even when React runs effects twice in development.
  const planned = useRef<string | null>(null);
  useEffect(() => {
    const key = `${session.id}|${since ?? ""}`;
    if (planned.current === key) return;
    planned.current = key;
    setPlan(null);
    void request(true);
  }, [session.id, since]);

  // The live copy of the run (from events) wins over the response we got back.
  const run = runId && live?.id === runId && !live.dry_run ? live : null;

  // Events are the main feed; poll too while running so a dropped stream can't freeze the screen.
  useEffect(() => {
    if (!run || run.status !== "running") return;
    const t = setInterval(() => {
      client.getRewind(run.id).then(onJob).catch(() => {});
    }, 1500);
    return () => clearInterval(t);
  }, [client, run?.id, run?.status, onJob]);

  return (
    <div ref={top} className="rewind">
      <button className="link-button back" onClick={onClose}>
        ← Back to the round
      </button>
      {error && (
        <p className="error banner" role="alert">
          {error}
        </p>
      )}
      {!run && !plan && !error && <p className="quiet loading">Drawing up the plan…</p>}
      {!run && plan && <Plan job={plan} actions={actions} from={from} busy={busy} onRun={() => request(false)} onClose={onClose} />}
      {run && run.status === "running" && <Running job={run} actions={actions} />}
      {run && run.status !== "running" && <Proof job={run} actions={actions} session={session} onClose={onClose} />}
    </div>
  );
}

// --- The plan --------------------------------------------------------------------------------

function Plan(props: {
  job: RewindJob;
  actions: Record<string, Action>;
  from?: Action;
  busy: boolean;
  onRun: () => void;
  onClose: () => void;
}) {
  const { job, actions, from, busy, onRun, onClose } = props;
  const counts = planCounts(job, actions);
  const held = job.steps.filter((s) => stepKind(s, actions[s.action_id]) === "held");
  const bySystem = SYSTEM_ORDER.map((system) => ({
    system,
    steps: job.steps.filter((s) => s.system === system && stepKind(s, actions[s.action_id]) !== "held"),
  })).filter((g) => g.steps.length);

  return (
    <section className="plan" aria-labelledby="plan-title">
      <p className="meta">
        dry run · plan {shortId(job.id)} · {job.steps.length} steps, newest first
        {from && (
          <span className="back-to">
            · back to {from.tool} at {new Date(from.created_at).toLocaleTimeString([], { hour12: false })}
          </span>
        )}
      </p>
      <div className="title-row">
        <h1 id="plan-title" className="headline">
          Here’s the do-over.
        </h1>
        <span className="sticker sticker-mint tilt-right">nothing’s moved yet</span>
      </div>
      <p className="lede">{planSentence(counts)}</p>

      {bySystem.map(({ system, steps }) => (
        <PlanSection key={system} system={system} title={systemName(system)} count={steps.length}>
          {steps.map((s) => (
            <StepRows key={s.action_id} step={s} action={actions[s.action_id]} />
          ))}
        </PlanSection>
      ))}
      {held.length > 0 && (
        <PlanSection system="mulligan" title="Held at the tee" count={held.length} caption="never ran">
          {held.map((s) => {
            const a = actions[s.action_id];
            return (
              <Row
                key={s.action_id}
                field={`${s.tool}${a?.approval ? ` · ${a.approval.code}` : ""}`}
                from="held"
                to="refused"
                note={s.detail ?? "It never ran, so there's nothing to undo."}
              />
            );
          })}
        </PlanSection>
      )}

      <footer className="plan-footer">
        <button className="pill-button big" disabled={busy || job.steps.length === 0} onClick={onRun}>
          <RewindGlyph size={24} /> {busy ? "Starting…" : "Take the mulligan"}
        </button>
        <button className="link-button" onClick={onClose}>
          Not now
        </button>
        <p className="quiet right">Runs newest first. Every system gets checksummed when it’s done.</p>
      </footer>
    </section>
  );
}

function PlanSection(props: { system: System; title: string; count: number; caption?: string; children: React.ReactNode }) {
  const { system, title, count, caption, children } = props;
  return (
    <div className="plan-section">
      <div className="plan-side">
        <SystemObject system={system} size={78} />
        <h2>{title}</h2>
        <span className="quiet small">{caption ?? `${count} ${count === 1 ? "step" : "steps"}`}</span>
      </div>
      <div className="plan-rows">{children}</div>
    </div>
  );
}

function StepRows({ step, action }: { step: RewindStep; action?: Action }) {
  if (step.changes.length === 0) {
    return <Row field={step.tool} from={action?.status ?? "done"} to="undone" note={step.detail ?? "back to before"} />;
  }
  return (
    <>
      {step.changes.map((c, i) => (
        <ChangeRow key={c.field} change={c} note={i === 0 ? step.detail : null} />
      ))}
      {step.changes_truncated && <p className="row-more">More rows, all back to before</p>}
    </>
  );
}

function ChangeRow({ change, note }: { change: FieldChange; note: string | null }) {
  const kept = change.outcome === "keep_human_edit";
  return (
    <Row
      field={fieldLabel(change.field)}
      sub={kept ? `was ${showValue(change.before)} before the agent` : undefined}
      from={showValue(change.agent_value)}
      to={showValue(kept ? change.current : change.before)}
      kept={kept}
      note={kept ? "A person changed this after the agent. Keeping theirs." : (note ?? (change.outcome === "skip" ? "left alone" : "back to before"))}
    />
  );
}

function Row(props: { field: string; sub?: string; from: string; to: string; note: string; kept?: boolean }) {
  const { field, sub, from, to, note, kept } = props;
  return (
    <div className={`row ${kept ? "row-kept" : ""}`}>
      <div className="row-field">
        <span className="mono">{field}</span>
        {sub && <span className="quiet small">{sub}</span>}
      </div>
      <div className="row-diff">
        <s className="mono agent">{from}</s>
        <span className="arrow" aria-label="becomes">→</span>
        <span className={`mono ${kept ? "sticker sticker-sun kept" : "strong"}`}>{to}</span>
      </div>
      <p className={`row-note ${kept ? "sun" : "quiet"}`}>
        {kept && <span className="person" aria-hidden>●</span>}
        {note}
      </p>
    </div>
  );
}

// --- The live run ----------------------------------------------------------------------------

const RESULT_TEXT: Record<RewindStep["result"], string> = {
  pending: "waiting",
  restored: "restored ✓",
  kept_human_edit: "kept a human edit",
  compensated: "offset ✓",
  skipped: "refused",
  failed: "failed",
};

function Running({ job, actions }: { job: RewindJob; actions: Record<string, Action> }) {
  const pending = job.steps.filter((s) => s.result === "pending");
  const done = job.steps.filter((s) => s.result !== "pending");
  const current = pending[0];
  const left = String(Math.min(999, pending.length)).padStart(3, "0");
  const doneSystems = [...new Set(done.map((s) => s.system))];
  const waiting = [...new Set(pending.slice(1).map((s) => s.system))].filter((s) => s !== current?.system);

  return (
    <section className="running" aria-labelledby="running-title" aria-live="polite">
      <div className="running-hero">
        <div className="running-text">
          <p className="meta">rewind {shortId(job.id)} · running</p>
          <h1 id="running-title" className="headline">
            Rewinding…
          </h1>
          <div className="counter-row">
            <span className="counter" aria-label={`${pending.length} left`}>
              {left.split("").map((d, i) => (
                <span key={i} className={`digit ${i === 2 ? "digit-sun" : ""}`}>
                  {d}
                </span>
              ))}
            </span>
            <span>changes left to put back</span>
          </div>
        </div>
        <div className="flight" aria-hidden>
          <DitherField mode="rewinding" className="flight-dither" />
          <svg className="arc" viewBox="0 0 664 380" preserveAspectRatio="none">
            <path d="M600 70 C 470 -20, 200 20, 110 320" />
            <path className="tee-line" d="M40 344 h200" />
          </svg>
          {current && <SystemObject key={current.action_id} system={current.system} size={130} className="in-flight" />}
          <div className="waiting">
            {waiting.slice(0, 3).map((s) => (
              <SystemObject key={s} system={s} size={70} className="dim" />
            ))}
          </div>
          <div className="tee">
            {doneSystems.slice(-4).map((s, i) => (
              <SystemObject key={s} system={s} size={92} className="landed" style={{ animationDelay: `${i * 80}ms` }} />
            ))}
            {doneSystems.length > 0 && <span className="sticker sticker-mint sticker-small">back · {done.length}</span>}
          </div>
          <span className="tee-label quiet small">the tee: how things were before</span>
        </div>
      </div>
      <Printout steps={job.steps} actions={actions} />
    </section>
  );
}

function Printout({ steps, actions }: { steps: RewindStep[]; actions: Record<string, Action> }) {
  const shown = steps.filter((s, i) => s.result !== "pending" || i === steps.findIndex((x) => x.result === "pending"));
  return (
    <div className="printout">
      <header className="section-head ruled">
        <h2>What’s come back</h2>
        <span className="quiet">newest first</span>
      </header>
      <ol>
        {shown.map((s) => {
          const kind = stepKind(s, actions[s.action_id]);
          const now = s.result === "pending";
          return (
            <li key={s.action_id} className={`print-row result-${s.result} ${now ? "is-now" : ""}`}>
              <span className="mono quiet">#{shortId(s.action_id)}</span>
              <span className="print-what">
                {systemName(s.system)} · {actions[s.action_id]?.summary ?? s.tool}
                {s.kept_fields.length > 0 && <span className="sticker sticker-sun sticker-small">kept a human edit</span>}
              </span>
              <span className="print-result small">{now ? "in flight" : kind === "held" ? "refused" : RESULT_TEXT[s.result]}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

// --- The proof -------------------------------------------------------------------------------

function Proof(props: { job: RewindJob; actions: Record<string, Action>; session: Session; onClose: () => void }) {
  const { job, actions, session, onClose } = props;
  const checks = Object.entries(job.verification) as [System, RewindJob["verification"][string]][];
  const ok = checks.filter(([, v]) => v.ok === true).length;
  const allOk = checks.length > 0 && ok === checks.length;
  const kept = job.steps.some((s) => s.kept_fields.length > 0);
  const systems = SYSTEM_ORDER.filter((s) => job.steps.some((x) => x.system === s) || job.verification[s]);
  const title = job.status === "failed" ? "The rewind stopped." : allOk ? "Back on the tee." : "Mostly back.";

  return (
    <section className="proof" aria-labelledby="proof-title">
      <p className="meta center">
        rewind {shortId(job.id)} · {job.status} · {job.steps.length} steps
      </p>
      <div className="title-row center">
        <h1 id="proof-title" className="headline">
          {title}
        </h1>
        {checks.length > 0 && (
          <span className={`sticker tilt-left ${allOk ? "sticker-mint" : "sticker-berry"}`}>
            {ok}/{checks.length} match
          </span>
        )}
      </div>
      <p className="lede center">
        {job.error ??
          "Every system was hashed again and compared with how it looked before the agent, with any kept human edits on top."}
      </p>
      <div className="lined-up" aria-hidden>
        {systems.map((s, i) => (
          <SystemObject key={s} system={s} size={110} className="landed" style={{ animationDelay: `${i * 120}ms` }} />
        ))}
      </div>
      <div className="proof-body">
        <div className="checksums">
          <header className="section-head ruled">
            <h2>The checksums</h2>
            <span className="quiet">expected, then actual</span>
          </header>
          {checks.length === 0 && <p className="quiet empty">No checksums were taken for this rewind.</p>}
          {checks.map(([system, v]) => (
            <div key={system} className="check">
              <SystemObject system={system} size={40} />
              <strong>{systemName(system)}</strong>
              <div className="hashes mono">
                <span className="quiet">{groupHash(v.expected)}</span>
                <span>{groupHash(v.actual)}</span>
              </div>
              <span className={`verdict ${v.ok === true ? "ok" : v.ok === false ? "bad" : "quiet"}`}>
                {v.ok === true ? <Tick /> : v.ok === false ? <Cross /> : null}
                {v.ok === true ? "match" : v.ok === false ? "no match" : "not checked"}
              </span>
            </div>
          ))}
          {kept && (
            <p className="sun note">
              <span className="person" aria-hidden>●</span> Expected includes the human edits that were kept, so they don’t count as a
              mismatch.
            </p>
          )}
        </div>
        <aside className="summary">
          <header className="section-head ruled">
            <h2>In one line</h2>
          </header>
          <p className="bubble">{rewindSummary(job, actions, `round ${shortId(session.id)}`)}</p>
          <button className="pill-button small" onClick={onClose}>
            Back to the round
          </button>
        </aside>
      </div>
    </section>
  );
}

const Tick = () => (
  <svg width={26} height={26} viewBox="0 0 28 28" aria-hidden>
    <circle cx={14} cy={14} r={13} fill="#5ED39A" />
    <path d="M8 14.5l4 4 8-9" stroke="#0B0B0A" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round" fill="none" />
  </svg>
);
const Cross = () => (
  <svg width={26} height={26} viewBox="0 0 28 28" aria-hidden>
    <circle cx={14} cy={14} r={13} fill="#FF5C6C" />
    <path d="M9 9l10 10M19 9L9 19" stroke="#0B0B0A" strokeWidth={3} strokeLinecap="round" />
  </svg>
);

