// Links and screenshots for the sponsor-evidence page (#/evidence). Only real artifacts go here:
// leave a field null until there's a real URL or file, and the page shows an empty slot instead.
// Screenshots live in dashboard/public/evidence/ and are served at /evidence/<name>.

export const REPO = "https://github.com/Manas-33/mulligan";

export type Shot = { src: string; alt: string; caption: string };

export const EVIDENCE = {
  sentry: {
    /** The ValueError event in the mulligan-gateway project. */
    crashEventUrl: "https://mulligan.sentry.io/issues/7798347560/" as string | null,
    /** Agent actions as mulligan.action spans (Explore → Traces, span.op:mulligan.action). */
    actionSpansUrl: "https://mulligan.sentry.io/explore/traces/?query=span.op%3Amulligan.action&statsPeriod=14d" as string | null,
    /** The commit the crash led to (a/p4-e2e-fixes). */
    fixCommit: "e71db3ffaab2f35ad6cc02cda09415151be6689c",
    shots: [
      {
        src: "/evidence/sentry-crash.jpg",
        alt: "Sentry issue MULLIGAN-GATEWAY-5: the parse_servers frame, with spec = '# e.g. slack=http://localhost:8200/mcp' in its local variables",
        caption: "MULLIGAN-GATEWAY-5. The frame’s local variables show the comment arriving as the setting’s value.",
      },
      {
        src: "/evidence/sentry-action-spans.png",
        alt: "Sentry Explore: mulligan.action spans for Postgres, GitHub and Notion tools in one demo run",
        caption: "One demo run: a span per agent action, across Postgres, GitHub and Notion.",
      },
    ] satisfies Shot[],
  },
  paper: {
    /** Share link for the Paper file "Mulligan Dashboard". */
    fileUrl: null as string | null,
    shots: [
      { src: "/evidence/paper-round.png", alt: "Paper design: the round screen", caption: "The round: what the agent did, and what’s held." },
      { src: "/evidence/paper-plan.png", alt: "Paper design: the rewind plan", caption: "The plan: a dry run, nothing has moved yet." },
      { src: "/evidence/paper-running.png", alt: "Paper design: the live rewind", caption: "The run: objects fly back to the tee." },
      { src: "/evidence/paper-proof.png", alt: "Paper design: the checksum proof", caption: "The proof: every system hashed again." },
    ] satisfies Shot[],
  },
};
