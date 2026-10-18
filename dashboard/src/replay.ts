// Session Replay, loaded lazily by instrument.ts. It masks all text and blocks media by default:
// the dashboard shows customer data.
import { replayIntegration } from "@sentry/react";

export const replay = () => replayIntegration();
