// Links and screenshots for the sponsor-evidence page (#/evidence). Only real artifacts go here:
// leave a field null until there's a real URL or file, and the page shows an empty slot instead.
// Screenshots live in dashboard/public/evidence/ and are served at /evidence/<name>.

export const REPO = "https://github.com/Manas-33/mulligan";

export type Shot = { src: string; alt: string; caption: string };

export const EVIDENCE = {
  sentry: {
    /** The ValueError event in the mulligan-gateway project. */
    crashEventUrl: null as string | null,
    /** A rewind's trace: mulligan.rewind with its mulligan.rewind.step spans across systems. */
    rewindTraceUrl: null as string | null,
    /** The commit the crash led to (a/p4-e2e-fixes). */
    fixCommit: "e71db3ffaab2f35ad6cc02cda09415151be6689c",
    shots: [
      { src: "/evidence/sentry-crash.png", alt: "Sentry issue: ValueError bad upstream server name '# e.g. slack'", caption: "The startup crash, as Sentry caught it." },
      { src: "/evidence/sentry-rewind-trace.png", alt: "Sentry trace of a rewind with one span per step", caption: "One rewind: a span per step, across Postgres, GitHub and Notion." },
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
