// Sentry, imported first by main.ts. Off when SENTRY_DSN_PHOTON isn't set. Only errors are sent:
// the reply and summary loops and the notify routes report through `report`, tagged by where they
// happened. No message text or phone handles go in: approvals carry customer data.

import * as Sentry from "@sentry/node";
import { loadEnv } from "./config.ts";

const env = loadEnv();
const dsn = env.SENTRY_DSN_PHOTON?.trim();

if (dsn) {
  Sentry.init({
    dsn,
    environment: env.SENTRY_ENVIRONMENT?.trim() || "dev",
    release: "mulligan-approvals-photon@0.1.0",
    tracesSampleRate: 0,
  });
}

export const sentryEnabled = Boolean(dsn);

/** Report an error to Sentry, tagged with the part of the service it came from. */
export function report(error: unknown, where: "reply-loop" | "summary-loop" | "notify", extra: Record<string, string> = {}): void {
  if (!dsn) return;
  Sentry.captureException(error, { tags: { where, ...extra } });
}

/** Send anything queued before the process exits. */
export const flush = (ms = 2000): Promise<boolean> => (dsn ? Sentry.close(ms) : Promise.resolve(true));
