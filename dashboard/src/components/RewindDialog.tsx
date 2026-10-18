// Rewind a session: dry run first (the plan, with per-field diffs), then run it and watch the
// steps land live (rewind.step events), ending with the checksum proof per system.

import { useState } from "react";
import { ApiError, type Client } from "../api/client.ts";
import type { FieldChange, RewindJob, RewindStep, Session } from "../types.ts";

const RESULT_LABEL: Record<RewindStep["result"], string> = {
  pending: "will undo",
  restored: "restored",
  kept_human_edit: "restored, kept a person's edit",
  compensated: "offset",
  skipped: "skipped",
  failed: "failed",
};

const show = (v: unknown) => (v === null || v === undefined ? "∅" : typeof v === "string" ? v : JSON.stringify(v));

function Change({ c }: { c: FieldChange }) {
  return (
    <tr className={`change change-${c.outcome}`}>
      <td>
        <code>{c.field}</code>
      </td>
      <td>{show(c.before)}</td>
      <td>{show(c.agent_value)}</td>
      <td>{show(c.current)}</td>
      <td>{c.outcome === "keep_human_edit" ? "keep person's edit" : c.outcome}</td>
    </tr>
  );
}

function Step({ step }: { step: RewindStep }) {
  return (
    <li className={`step step-${step.result}`}>
      <div className="step-head">
        <span className={`lane lane-${step.system}`}>{step.system}</span>
        <code>{step.tool}</code>
        <span className={`pill pill-${step.result}`}>{RESULT_LABEL[step.result]}</span>
      </div>
      {step.detail && <p>{step.detail}</p>}
      {step.kept_fields.length > 0 && (
        <p className="kept">Kept {step.kept_fields.length} field(s) a person changed after the agent.</p>
      )}
      {step.changes.length > 0 && (
        <details open={step.kept_fields.length > 0}>
          <summary>{step.changes.length} field(s)</summary>
          <table className="diff">
            <thead>
              <tr>
                <th>field</th>
                <th>before</th>
                <th>agent wrote</th>
                <th>now</th>
                <th>outcome</th>
              </tr>
            </thead>
            <tbody>
              {step.changes.map((c) => (
                <Change key={c.field} c={c} />
              ))}
            </tbody>
          </table>
          {step.changes_truncated && <p className="muted">More changes not shown.</p>}
        </details>
      )}
    </li>
  );
}

export function Verification({ job }: { job: RewindJob }) {
  const systems = Object.entries(job.verification);
  if (systems.length === 0) return null;
  return (
    <div className="verify">
      <h3>Checksum proof</h3>
      <ul>
        {systems.map(([system, v]) => (
          <li key={system} className={v.ok === true ? "ok" : v.ok === false ? "bad" : ""}>
            <strong>{system}</strong>{" "}
            {v.ok === null ? "not checked yet" : v.ok ? "matches the state before the agent" : "does NOT match"}
            <div className="hash">
              expected <code>{v.expected.slice(0, 12)}</code> · actual <code>{v.actual?.slice(0, 12) ?? "–"}</code>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RewindDialog(props: { client: Client; session: Session; live?: RewindJob; onClose: () => void; onJob: (j: RewindJob) => void }) {
  const { client, session, live, onClose, onJob } = props;
  const [plan, setPlan] = useState<RewindJob | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Once a run starts, its live copy (from events) supersedes the plan.
  const job = live && live.id !== plan?.id && !live.dry_run ? live : plan;
  const running = job?.status === "running";
  const done = job && !job.dry_run && job.status !== "running";

  const call = async (dryRun: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const j = await client.rewind({ session_id: session.id }, dryRun);
      onJob(j);
      if (dryRun) setPlan(j);
    } catch (e) {
      setError(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="overlay" role="dialog" aria-modal="true" aria-label="Rewind session">
      <div className="dialog">
        <header>
          <h2>Rewind {session.agent.display_name}'s session</h2>
          <button className="secondary" onClick={onClose}>
            Close
          </button>
        </header>
        {!job && (
          <p>
            See exactly what a rewind would undo before anything changes. Steps run newest first, and fields a person
            changed after the agent are kept.
          </p>
        )}
        {error && <p className="error">{error}</p>}
        {job && (
          <>
            <p className="muted">
              {job.dry_run ? "Plan (nothing changed yet)" : `Rewind ${job.status}`} · {job.steps.length} step(s)
              {job.error && <span className="error"> · {job.error}</span>}
            </p>
            <ol className="steps">
              {job.steps.map((s) => (
                <Step key={s.action_id} step={s} />
              ))}
            </ol>
            {!job.dry_run && <Verification job={job} />}
          </>
        )}
        <footer>
          {!plan && !done && (
            <button disabled={busy} onClick={() => call(true)}>
              Preview rewind
            </button>
          )}
          {plan && !done && !running && (
            <button disabled={busy} className="danger" onClick={() => call(false)}>
              Rewind {plan.steps.length} action(s)
            </button>
          )}
          {running && <span>Rewinding…</span>}
        </footer>
      </div>
    </div>
  );
}
