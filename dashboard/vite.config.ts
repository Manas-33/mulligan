import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Read the repo-root .env, but expose only these names to browser code. Each is meant for the
  // dashboard (the DSN is public, the token is the dashboard's own). Nothing else in .env leaks.
  envDir: "..",
  envPrefix: ["VITE_", "MULLIGAN_DASHBOARD_TOKEN", "SENTRY_DSN_DASHBOARD", "SENTRY_ENVIRONMENT"],
  // 127.0.0.1 on the host; the Dockerfile overrides with --host 0.0.0.0 inside the container.
  server: { host: "127.0.0.1", port: 5173, strictPort: true },
  preview: { host: "127.0.0.1", port: 5173, strictPort: true },
});
