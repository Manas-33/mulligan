# Events

The gateway publishes every state change on an internal bus and streams the same events to `GET /api/events` (SSE). Names and payloads are identical in both places. The envelope types are in `action.schema.json` (`MulliganEvent` and the five `*Event` types), and in the generated `models.py` and `types.ts`.

## Envelope

```json
{ "event": "action.held", "seq": 41, "ts": "2026-10-17T23:04:28Z", "data": { "...": "an Action" } }
```

- `event`: one of the names below.
- `seq`: a positive integer that increases by one per event, across the whole gateway. Use it to order events and to resume.
- `ts`: when the event happened (RFC 3339, UTC).
- `data`: the full object after the change, never a diff. Replace your copy with it.

## Events

| Event | `data` | When |
|---|---|---|
| `session.started` | `Session` | First MCP request with a new `Mcp-Session-Id` |
| `session.ended` | `Session` | The client closes the session, or it is idle for 30 minutes |
| `action.created` | `Action` | A tool call arrives. Status `pending` |
| `action.classified` | `Action` | Its `reversibility` is set, from the policy table or the rules gate |
| `action.held` | `Action` | It waits for approval. Status `held`, `approval` filled in |
| `action.approved` | `Action` | Someone approved it. Status `approved` |
| `action.rejected` | `Action` | Someone rejected it, or a rewind rejected it while held. Status `rejected` |
| `action.executed` | `Action` | It ran. Status `executed`, `after_hashes` and `inverse` filled in |
| `action.failed` | `Action` | It failed. Status `failed`, `error` set |
| `rewind.planned` | `RewindJob` | A dry run finished. Status `planned` |
| `rewind.started` | `RewindJob` | A run started. Status `running` |
| `rewind.step` | `RewindStepData` (`{rewind_job_id, step, action}`) | One step finished. `action` has its new status: `reverted` (step `restored` or `compensated`), `conflict` or `revert_failed` |
| `rewind.completed` | `RewindJob` | A run finished. Status `completed` or `partial` |
| `rewind.failed` | `RewindJob` | A run stopped early. Status `failed`, `error` set |
| `checksum.verified` | `ChecksumVerifiedData` (`{system, expected, actual, ok, rewind_job_id}`) | A system's checksum was compared after a rewind (or by the demo) |
| `hold.released` | `Action` | A held action's hold ended and it went ahead: the email sent, or the payment was captured |
| `hold.expired` | `Action` | Nobody decided in time. Status `expired` |

A held action that is approved emits `action.approved`, then `hold.released` and `action.executed` once it runs. A payment emits `action.executed` when it is authorized, and `hold.released` later when it is captured.

## SSE framing

```
id: 41
event: action.held
data: {"event":"action.held","seq":41,"ts":"2026-10-17T23:04:28Z","data":{...}}

```

- `id` is the envelope's `seq`.
- A `: ping` comment line is sent every 15 seconds so proxies keep the connection open.
- To resume, reconnect with the `Last-Event-ID` header set to the last `seq` you saw. The gateway replays newer events from its `events` table (up to the last 1,000), then continues live. Without the header you get only new events, so load current state from the REST API first.
- Auth is the same as the rest of the API. Browsers' `EventSource` can't send headers, so the dashboard uses a fetch-based SSE client (for example `@microsoft/fetch-event-source`) with `Authorization: Bearer <dashboard token>`.

## Internal bus

In the gateway, `EventBus.publish(event, data)` (see `gateway/mulligan/interfaces.py`) assigns `seq` and `ts`, writes the event to the `events` table, and fans it out to SSE subscribers. Stream B's `HoldManager` publishes `action.held`, `action.approved`, `action.rejected`, `hold.released` and `hold.expired` through it. Everything else comes from the core.
