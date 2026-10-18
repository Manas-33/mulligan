// Build-time settings, inlined by Vite. vite.config.ts reads the repo-root .env and exposes only
// the names listed in its envPrefix, all of which are meant for the browser on the demo laptop.
const env = import.meta.env;
// `||`, not `??`: a name present in .env but left empty means "not set".

/** The gateway REST API. Defaults to the Prism mock (make mocks); set it to http://127.0.0.1:8000 for the real gateway. */
export const API_URL: string = env.VITE_MULLIGAN_API_URL || "http://127.0.0.1:4010";
export const DASHBOARD_TOKEN: string =
  env.VITE_MULLIGAN_DASHBOARD_TOKEN || env.MULLIGAN_DASHBOARD_TOKEN || "dev-dashboard-token";
export const SENTRY_DSN: string | undefined = env.SENTRY_DSN_DASHBOARD || undefined;
export const SENTRY_ENVIRONMENT: string = env.SENTRY_ENVIRONMENT || "dev";
