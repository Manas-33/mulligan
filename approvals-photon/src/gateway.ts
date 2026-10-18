// The gateway REST calls the service makes (contracts/openapi.yaml), with the service token.

import type { Action, ApprovalDecisionRequest, RewindJob } from "./types.ts";

export interface Gateway {
  heldActions(): Promise<Action[]>;
  /** Returns the action after the decision, or throws GatewayError (404 / 409 not held / 422 wrong code). */
  decide(actionId: string, body: ApprovalDecisionRequest): Promise<Action>;
  /** The newest rewind jobs, newest first (dry runs included). */
  rewinds(limit?: number): Promise<RewindJob[]>;
}

export class GatewayError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`gateway ${status}: ${detail}`);
  }
}

export function httpGateway(baseUrl: string, serviceToken: string, doFetch: typeof fetch = fetch): Gateway {
  const call = async <T>(path: string, init: RequestInit = {}): Promise<T> => {
    const res = await doFetch(new URL(path, baseUrl), {
      ...init,
      headers: { "content-type": "application/json", "x-mulligan-service-token": serviceToken, ...init.headers },
    });
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    if (!res.ok) throw new GatewayError(res.status, typeof body?.detail === "string" ? body.detail : res.statusText);
    return body as T;
  };
  return {
    heldActions: () => call<Action[]>("/api/actions?status=held"),
    decide: (actionId, body) =>
      call<Action>(`/api/approvals/${encodeURIComponent(actionId)}/decision`, { method: "POST", body: JSON.stringify(body) }),
    rewinds: (limit = 20) => call<RewindJob[]>(`/api/rewind?limit=${limit}`),
  };
}
