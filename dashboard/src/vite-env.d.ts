/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_MULLIGAN_API_URL?: string;
  readonly VITE_MULLIGAN_DASHBOARD_TOKEN?: string;
  readonly MULLIGAN_DASHBOARD_TOKEN?: string;
  readonly SENTRY_DSN_DASHBOARD?: string;
  readonly SENTRY_ENVIRONMENT?: string;
}
