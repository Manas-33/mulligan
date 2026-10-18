// Sentry, imported first by main.tsx. Off when SENTRY_DSN_DASHBOARD isn't set.
import * as Sentry from "@sentry/react";
import { API_URL, SENTRY_DSN, SENTRY_ENVIRONMENT } from "./config.ts";

if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: SENTRY_ENVIRONMENT,
    release: "mulligan-dashboard@0.1.0",
    integrations: [
      Sentry.browserTracingIntegration(),
      // Replay masks all text and blocks media by default: the dashboard shows customer data.
      Sentry.replayIntegration(),
    ],
    tracesSampleRate: 1.0,
    // Link dashboard requests to the gateway's traces.
    tracePropagationTargets: [API_URL],
    // A demo laptop with a handful of sessions: record them all.
    replaysSessionSampleRate: 1.0,
    replaysOnErrorSampleRate: 1.0,
  });
}
