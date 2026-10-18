// "Held at the tee": actions waiting on a person, with why they were held and the code to text back.

import { useEffect, useState } from "react";
import { ApiError, type Client } from "../api/client.ts";
import type { Action, ApprovalDecision } from "../types.ts";
import { systemName, timeLeft } from "../view.ts";
import { Flag, SystemObject } from "./SystemObject.tsx";

function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

/** Why an action was held, in plain words, when the classifier didn't give a reason. */
function whyHeld(a: Action): string {
  if (a.reversibility.reason) return a.reversibility.reason;
  if (a.hold?.kind === "payment_auth") return "Money would move. It's only authorized so far, and nothing is taken unless you say so.";
  if (a.hold?.kind === "email_outbox") return "It can't be unsent, so it waits in the outbox until you decide.";
  if (a.upstream_server) return "Mulligan can't undo this tool, so it asks first.";
  return "This can't be undone, so a person decides.";
}

export function Holds({ client, held }: { client: Client; held: Action[] }) {
  const now = useNow();
  return (
    <section className="holds" aria-labelledby="holds-title">
      <header className="section-head ruled">
        <h2 id="holds-title">Held at the tee</h2>
        <span className={held.length ? "berry" : "quiet"}>{held.length ? `${held.length} need you` : "all clear"}</span>
      </header>
      {held.length === 0 ? (
        <p className="quiet empty">Nothing is waiting. Anything that can't be undone stops here first.</p>
      ) : (
        <ol className="hold-list">
          {held.map((a) => (
            <Hold key={a.id} client={client} action={a} now={now} />
          ))}
        </ol>
      )}
    </section>
  );
}

function Hold({ client, action, now }: { client: Client; action: Action; now: number }) {
  const [busy, setBusy] = useState<ApprovalDecision | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const left = timeLeft(action.approval?.expires_at, now);

  const decide = async (decision: ApprovalDecision) => {
    setBusy(decision);
    setErr(null);
    try {
      await client.decide(action, decision); // the SSE event moves the row out of this list
    } catch (e) {
      setErr(e instanceof ApiError ? String(e.detail) : String(e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <li className="hold">
      <SystemObject system={action.system} size={92} className="hop" />
      <div className="hold-body">
        <p className="mono berry hold-meta">
          <Flag size={14} /> {systemName(action.system).toLowerCase()}
          {action.upstream_server && <> · {action.upstream_server}</>}
          {left && <> · {left}</>}
        </p>
        <h3>{action.summary ?? action.tool}</h3>
        <p className="quiet">{whyHeld(action)}</p>
        <div className="hold-actions">
          <button className="pill-button small" disabled={busy !== null} onClick={() => decide("approve")}>
            {busy === "approve" ? "Letting it through…" : "Let it through"}
          </button>
          <button className="link-button" disabled={busy !== null} onClick={() => decide("reject")}>
            {busy === "reject" ? "Refusing…" : "Refuse"}
          </button>
          {action.approval && (
            <span className="code" title="Text this code back to approve from your phone">
              {action.approval.code}
            </span>
          )}
        </div>
        {err && <p className="error" role="alert">{err}</p>}
      </div>
    </li>
  );
}
