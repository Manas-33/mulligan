// Typed client for the gateway REST API (contracts/openapi.yaml). Every route but /healthz needs
// `Authorization: Bearer <dashboard token>`. Live updates (SSE /api/events) come in Phase 2.

import type { Action, ActionStatus, Checksums, Session, SessionStatus, System } from "../types.ts";

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
  async function get<T>(path: string, query: Record<string, string | undefined> = {}): Promise<T> {
    const url = new URL(path, baseUrl);
    for (const [key, value] of Object.entries(query)) if (value !== undefined) url.searchParams.set(key, value);
    const res = await doFetch(url, { headers: { Authorization: `Bearer ${token}` } });
    const body: unknown = await res.json().catch(() => null);
    if (!res.ok) throw new ApiError(res.status, (body as { detail?: unknown } | null)?.detail ?? body);
    return body as T;
  }

  return {
    health: () => get<{ ok: boolean }>("/healthz"),
    listSessions: (status?: SessionStatus) => get<Session[]>("/api/sessions", { status }),
    listActions: (query: ActionQuery = {}) => get<Action[]>("/api/actions", query),
    getAction: (id: string) => get<Action>(`/api/actions/${encodeURIComponent(id)}`),
    getChecksums: () => get<Checksums>("/api/checksums"),
  };
}

export type Client = ReturnType<typeof createClient>;
