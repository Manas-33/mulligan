// Phase 1 shell: proves the dashboard reaches the API (Prism mock or gateway) with the shared types.
// The real screens follow the Paper design in Phase 2.

import { useEffect, useState } from "react";
import { createClient } from "./api/client.ts";
import { API_URL, DASHBOARD_TOKEN } from "./config.ts";
import type { Session } from "./types.ts";

const client = createClient({ baseUrl: API_URL, token: DASHBOARD_TOKEN });

type State = { kind: "loading" } | { kind: "ok"; sessions: Session[] } | { kind: "error"; message: string };

export function App() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    client
      .listSessions()
      .then((sessions) => setState({ kind: "ok", sessions }))
      .catch((e: unknown) => setState({ kind: "error", message: e instanceof Error ? e.message : String(e) }));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", padding: 24, maxWidth: 720 }}>
      <h1>Mulligan</h1>
      <p>API: {API_URL}</p>
      {state.kind === "loading" && <p>Connecting…</p>}
      {state.kind === "error" && <p role="alert">Can't reach the API: {state.message}</p>}
      {state.kind === "ok" && (
        <ul>
          {state.sessions.map((s) => (
            <li key={s.id}>
              {s.agent.display_name}: {s.action_count} actions ({s.status}, {s.mode})
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
