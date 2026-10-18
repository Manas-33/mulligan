// Typed client for the gateway REST API (contracts/openapi.yaml). Every route but /healthz needs
// `Authorization: Bearer <dashboard token>`. Live updates (SSE /api/events) come in Phase 2.

import type {
  Action,
  ActionStatus,
  ApprovalDecision,
  Checksums,
  RewindJob,
  RewindScope,
  Session,
  SessionStatus,
  System,
} from "../types.ts";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(`gateway returned ${status}`);
  }
}

export type ClientOptions = { baseUrl: string; token: string; fetch?: typeof fetch };

export type ActionQuery = { session_id?: string; since?: string; system?: System; status?: ActionStatus };

export function createClient({ baseUrl, token, fetch: doFetch = fetch }: ClientOptions) {
  async function request<T>(
    path: string,
    { query = {}, body }: { query?: Record<string, string | undefined>; body?: unknown } = {},
  ): Promise<T> {
    const url = new URL(path, baseUrl);
    for (const [key, value] of Object.entries(query)) if (value !== undefined) url.searchParams.set(key, value);
    const res = await doFetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "content-type": "application/json" }) },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data: unknown = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, (data as { detail?: unknown } | null)?.detail ?? data);
    return data as T;
  }

  return {
    health: () => request<{ ok: boolean }>("/healthz"),
    listSessions: (status?: SessionStatus) => request<Session[]>("/api/sessions", { query: { status } }),
    listActions: (query: ActionQuery = {}) => request<Action[]>("/api/actions", { query }),
    getAction: (id: string) => request<Action>(`/api/actions/${encodeURIComponent(id)}`),
    getChecksums: () => request<Checksums>("/api/checksums"),
    /** Dry run (the plan, nothing changes) or run a rewind. A run returns status running; follow rewind.* events. */
    rewind: (scope: RewindScope, dryRun: boolean) =>
      request<RewindJob>("/api/rewind", { body: { scope, dry_run: dryRun, requested_by: "dashboard" } }),
    getRewind: (id: string) => request<RewindJob>(`/api/rewind/${encodeURIComponent(id)}`),
    /** Approve or reject a held action from the dashboard (needs its approval code). */
    decide: (action: Action, decision: ApprovalDecision) =>
      request<Action>(`/api/approvals/${encodeURIComponent(action.id)}/decision`, {
        body: { decision, code: action.approval?.code ?? "", channel: "dashboard", decided_by: "dashboard" },
      }),
  };
}

export type Client = ReturnType<typeof createClient>;
