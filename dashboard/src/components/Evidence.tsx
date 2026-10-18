// The sponsor-evidence page (#/evidence), written as a yardage book: a golfer's notes on each hole.
// Hole 1 is what Sentry showed us, hole 2 the Photon approval and summary texts, hole 3 the Paper
// design. Every claim points at code or a commit; links and screenshots come from evidence.ts.

import { useState, type ReactNode } from "react";
import { EVIDENCE, REPO, type Shot } from "../evidence.ts";
import type { System } from "../types.ts";
import { Flag, RewindGlyph, SystemObject } from "./SystemObject.tsx";

export default function Evidence() {
  return (
    <article className="evidence" aria-labelledby="evidence-title">
      <section className="hero state">
        <div className="hero-text">
          <p className="meta">how mulligan was built · 3 holes</p>
          <h1 id="evidence-title" className="headline">
            <span className="line">The yardage</span>
            <span className="line">
              <span className="sticker sticker-cream">book.</span>
            </span>
          </h1>
          <p className="lede">
            Notes from building Mulligan: what Sentry showed us, how a text message lets a person decide what an agent can’t undo,
            and where the look came from.
          </p>
          <p>
            <a className="link-button" href="#/">
              ← Back to the dashboard
            </a>
          </p>
        </div>
        <ol className="holes" aria-label="Holes">
          <HoleLink n={1} href="#hole-sentry" label="Sentry" />
          <HoleLink n={2} href="#hole-photon" label="Photon" />
          <HoleLink n={3} href="#hole-paper" label="Paper" />
        </ol>
      </section>

      <SentryHole />
      <PhotonHole />
      <PaperHole />
    </article>
  );
}

function HoleLink({ n, href, label }: { n: number; href: string; label: string }) {
  return (
    <li>
      <a href={href} onClick={jump(href)}>
        <Flag size={22} />
        <span className="mono quiet">hole {n}</span>
        <strong>{label}</strong>
      </a>
    </li>
  );
}

/** In-page links must not change the hash, which routes the app. */
const jump = (href: string) => (e: React.MouseEvent) => {
  e.preventDefault();
  document.querySelector(href)?.scrollIntoView({ behavior: "smooth", block: "start" });
};

function Hole({ id, n, title, par, children }: { id: string; n: number; title: string; par: string; children: ReactNode }) {
  return (
    <section id={id} className="hole" aria-labelledby={`${id}-title`}>
      <header className="hole-head">
        <span className="hole-number mono">{n}</span>
        <div>
          <h2 id={`${id}-title`}>{title}</h2>
          <p className="quiet">{par}</p>
        </div>
      </header>
      {children}
    </section>
  );
}

// --- Hole 1: Sentry --------------------------------------------------------------------------

function SentryHole() {
  const { crashEventUrl, rewindTraceUrl, fixCommit, shots } = EVIDENCE.sentry;
  return (
    <Hole id="hole-sentry" n={1} title="Sentry caught the gateway falling over" par="error → root cause → commit">
      <div className="notes">
        <ol className="steps">
          <Step mark="1" title="The gateway crashed on startup under Docker Compose.">
            Sentry was already on, so the crash came in as an issue with its stack trace:
            <pre className="mono trace-line">ValueError: bad upstream server name '# e.g. slack'</pre>
            {crashEventUrl && <ExtLink href={crashEventUrl}>Open the event in Sentry</ExtLink>}
          </Step>
          <Step mark="2" title="A comment had become a setting.">
            Compose reads an empty value followed by a comment as the comment itself. A value with text before the comment is
            fine. Checked with Compose 5.6.0:
            <pre className="mono repro">
              <span className="quiet"># .env</span>
              {"\n"}UPSTREAM_MCP_SERVERS=   # e.g. slack{"\n"}
              <span className="quiet">$ docker compose config</span>
              {"\n"}UPSTREAM_MCP_SERVERS: <span className="berry">'# e.g. slack'</span>
            </pre>
            <code className="mono">.env.example</code> had seven keys written that way. Only the upstream server list crashed; the
            others, including the rogue agent’s API key, would have started up with a comment as their value.
          </Step>
          <Step mark="3" title="The fix.">
            Comments moved to their own lines, and the gateway now treats any value starting with <code className="mono">#</code> as
            unset, whether it comes from <code className="mono">.env</code> or from Compose.{" "}
            <ExtLink href={`${REPO}/commit/${fixCommit}`}>
              commit <span className="mono">{fixCommit.slice(0, 7)}</span>
            </ExtLink>
          </Step>
        </ol>
        <Shots shots={shots.slice(0, 1)} />
      </div>

      <h3 className="notes-title">What else Sentry watches</h3>
      <ul className="products">
        <Product name="Tracing" where="gateway">
          A span for every agent action (<code className="mono">mulligan.action</code>) and for every rewind and each of its steps (
          <code className="mono">mulligan.rewind</code>, <code className="mono">mulligan.rewind.step</code>). The dashboard links each
          action to its trace, and sends trace headers with every request so the gateway’s spans continue the browser’s trace.
          {rewindTraceUrl && (
            <>
              {" "}
              <ExtLink href={rewindTraceUrl}>A rewind’s trace</ExtLink>
            </>
          )}
        </Product>
        <Product name="MCP monitoring" where="gateway">
          The gateway is an MCP server. With the <code className="mono">mcp</code> package installed, sentry-sdk turns on its MCP
          integration, which records a <code className="mono">tools/call</code> span for each tool call without the arguments. Spike
          0.8 checked this before we relied on it.
        </Product>
        <Product name="Session Replay" where="dashboard">
          Every dashboard session is recorded with all text masked and media blocked, since the screens show customer data. Replay
          loads after the page, so it doesn’t slow the first paint.
        </Product>
        <Product name="Errors" where="gateway · dashboard · Photon">
          All three services report to their own projects. The Photon service reports once per failure streak, so a gateway that’s
          down for an hour is one issue, not thousands.
        </Product>
      </ul>
      <div className="trace-shot">
        <Shots shots={shots.slice(1)} />
      </div>
    </Hole>
  );
}

function Step({ mark, title, children }: { mark: string; title: string; children: ReactNode }) {
  return (
    <li className="step">
      <span className="step-mark mono">{mark}</span>
      <div className="step-body">
        <h3>{title}</h3>
        <div className="quiet">{children}</div>
      </div>
    </li>
  );
}

function Product({ name, where, children }: { name: string; where: string; children: ReactNode }) {
  return (
    <li className="product">
      <p>
        <strong>{name}</strong> <span className="mono quiet small">{where}</span>
      </p>
      <p className="quiet">{children}</p>
    </li>
  );
}

// --- Hole 2: Photon --------------------------------------------------------------------------

function PhotonHole() {
  return (
    <Hole id="hole-photon" n={2} title="A text decides what can’t be undone" par="held → texted → YES 4821 → runs">
      <p className="lede hole-lede">
        Most agent actions run straight away, because Mulligan can rewind them. The few it can’t undo (an email, a charge, a tool it
        doesn’t know) wait at the tee until a person says yes. Photon carries that question over iMessage, and the answer back.
      </p>
      <div className="thread-wrap">
        <ol className="thread" aria-label="An approval over iMessage">
          <Beat system="email" who="agent">
            The agent calls <code className="mono">email_send</code> to 412 customers. Email can’t be unsent, so the gateway holds it
            and gives it a four-digit code.
          </Beat>
          <Text from="mulligan">
            Mulligan: Email 412 customers "Important update to your account"
            {"\n"}Reply YES 4821 to approve or NO 4821 to reject.
          </Text>
          <Text from="you">YES 4821</Text>
          <Beat system="mulligan" who="photon service">
            The reply loop reads new texts every second, finds the held action with code 4821 and records the decision through the
            gateway, marked as coming from iMessage. Then it answers.
          </Beat>
          <Text from="mulligan">Approved. It’s running now. (email_send, …)</Text>
        </ol>
        <ol className="thread" aria-label="A rewind summary over iMessage">
          <Beat system="mulligan" who="later">
            Someone takes the mulligan from the dashboard. The service watches rewind jobs and, when a run finishes, texts one line.
          </Beat>
          <Text from="mulligan">
            Mulligan: Rewound round m7c4. 53 changes undone, 2 held actions refused and kept 2 human edits. Postgres, GitHub and
            Notion checksums match.
          </Text>
          <Beat system="mulligan" who="why it’s safe">
            Each run is texted once: the job id is the message’s idempotency key, so a restart can’t double-text. The service keeps no
            state of its own; codes are looked up on the gateway each time.
          </Beat>
        </ol>
      </div>
    </Hole>
  );
}

function Beat({ system, who, children }: { system: System; who: string; children: ReactNode }) {
  return (
    <li className="beat">
      <SystemObject system={system} size={34} />
      <div>
        <p className="mono quiet small">{who}</p>
        <p className="quiet">{children}</p>
      </div>
    </li>
  );
}

function Text({ from, children }: { from: "mulligan" | "you"; children: ReactNode }) {
  return (
    <li className={`text text-${from}`}>
      <p className={`bubble ${from === "you" ? "bubble-you" : ""}`}>{children}</p>
      <span className="mono quiet small">{from === "you" ? "approver" : "imessage"}</span>
    </li>
  );
}

// --- Hole 3: Paper ---------------------------------------------------------------------------

function PaperHole() {
  const { fileUrl, shots } = EVIDENCE.paper;
  return (
    <Hole id="hole-paper" n={3} title="Drawn in Paper first" par="file “Mulligan Dashboard” · page “v4 · Canvas + objects”">
      <p className="lede hole-lede">
        A golf do-over needed a course, not a control panel. The design was drawn in Paper before any code: a black ground with
        faint mowing stripes, one hand-drawn object per system, Open Sans for words and Courier Prime for codes and hashes.
      </p>
      <div className="carried">
        <p className="carried-objects" aria-hidden>
          {(["postgres", "github", "notion", "email", "payments", "upstream"] as System[]).map((s) => (
            <SystemObject key={s} system={s} size={48} />
          ))}
        </p>
        <p className="quiet">
          The objects in this dashboard are the same SVG art as the Paper file, and the moving texture behind the timeline and the
          live rewind is Paper Shaders’ dither, rendered by <code className="mono">@paper-design/shaders-react</code>.
        </p>
      </div>
      {fileUrl && (
        <p>
          <ExtLink href={fileUrl}>Open the Paper file</ExtLink>
        </p>
      )}
      <Shots shots={shots} />
      <p className="sign-off quiet">
        <RewindGlyph size={18} /> Take a mulligan.
      </p>
    </Hole>
  );
}

// --- Shared ----------------------------------------------------------------------------------

function ExtLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a className="ext" href={href} target="_blank" rel="noreferrer">
      {children} ↗
    </a>
  );
}

/** Screenshots, each with an empty slot (naming the file to add) until the file exists. */
function Shots({ shots }: { shots: Shot[] }) {
  return (
    <div className="shots">
      {shots.map((s) => (
        <Screenshot key={s.src} shot={s} />
      ))}
    </div>
  );
}

function Screenshot({ shot }: { shot: Shot }) {
  const [missing, setMissing] = useState(false);
  return (
    <figure className="shot">
      {missing ? (
        <div className="shot-slot">
          <span className="quiet small">screenshot to add</span>
          <span className="mono small">dashboard/public{shot.src}</span>
        </div>
      ) : (
        <img src={shot.src} alt={shot.alt} loading="lazy" onError={() => setMissing(true)} />
      )}
      <figcaption className="quiet small">{shot.caption}</figcaption>
    </figure>
  );
}
