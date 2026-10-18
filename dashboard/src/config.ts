// Build-time settings (Vite inlines VITE_* at build). The dashboard runs in the browser on the
// demo laptop, so it talks to the gateway at 127.0.0.1:8000, or to the Prism mock by default.
export const API_URL: string = import.meta.env.VITE_MULLIGAN_API_URL ?? "http://127.0.0.1:4010";
export const DASHBOARD_TOKEN: string = import.meta.env.VITE_MULLIGAN_DASHBOARD_TOKEN ?? "dev-dashboard-token";
