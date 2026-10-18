# MCP tools

What agents see when they connect to Mulligan at `/mcp` on port 8001 (Streamable HTTP, `Authorization: Bearer <agent token>`). Each token maps to one `Agent` (`MULLIGAN_AGENT_TOKENS`), so the agent can't choose who it is. Agents never get a credential for a real system, and never see the REST API.

**Sessions.** A `Session` is one `Mcp-Session-Id`. Clients that open on the sessionless 2026-07-28 protocol are asked to use the `initialize` handshake instead, so they get a session id; a client that can't gets one session per agent token, which ends after 30 idle minutes.

Every built-in tool has an entry in `reversibility_policy.yaml`. The gateway won't start if one is missing.

## Common rules

- **Results.** Every result has `structuredContent` with at least `action_id` and `status`, and a text block with the action's one-line `summary`. A held call returns at once with `status: "held"`; it never blocks waiting for approval. The approval code is never sent to the agent.
- **Errors.** A bad argument or a failed call returns an MCP tool error (`isError: true`) with a short reason. The attempt is still logged as an action with status `failed`.
- **Redaction.** `args_redacted` drops values under keys that look secret (`token`, `secret`, `password`, `key`, `authorization`), cuts strings over 500 characters and summarizes arrays over 50 items (`"412 recipients"`).
- **Scope.** GitHub tools act only on `GITHUB_ORG/GITHUB_REPO`. Notion tools act only on pages shared with the integration. Postgres tools act only on schemas in `PG_ALLOWED_SCHEMAS` (default `public`).

## Postgres

Write tools take a table, a structured filter and values, never SQL, so Mulligan knows which rows a call touches before it runs it. Identifiers are checked against the catalog and quoted; values go in as one JSON parameter that Postgres converts to the column types (`jsonb_populate_record`). Each call runs in one transaction with `mulligan.action_id` set locally, and the audit trigger (`mulligan_audit.audit_log`) records the old and new row of every row it changes, including rows changed by `ON DELETE CASCADE` or `SET NULL`.

- A table needs a primary key, and a write is refused if it or any table an FK action can reach from it has no audit trigger, since those changes couldn't be undone.
- Agents can't set `GENERATED ALWAYS` columns.

`Filter` (all conditions are ANDed):

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": false,
    "required": ["column", "op"],
    "properties": {
      "column": { "type": "string" },
      "op": { "enum": ["eq", "ne", "lt", "lte", "gt", "gte", "in", "not_in", "is_null", "is_not_null", "like", "ilike"] },
      "value": { "description": "Required except for is_null / is_not_null. An array for in / not_in." }
    }
  }
}
```

An empty filter is rejected unless the call also sets `"all": true`.

| Tool | Input | Result (`structuredContent`) | Label |
|---|---|---|---|
| `pg_query` | `{sql: string (one SELECT, literals inline), max_rows?: 1..1000 = 200}` | `{columns: string[], rows: any[][], row_count, truncated}` | read_only |
| `pg_insert_rows` | `{table, rows: object[] (1..1000)}` | `{inserted, primary_keys: any[][]}` | reversible |
| `pg_update_rows` | `{table, filter: Filter, all?: bool, values: object (≥1 column)}` | `{updated}` | reversible |
| `pg_delete_rows` | `{table, filter: Filter, all?: bool}` | `{deleted, cascaded: {"<table>": count}}` (rows changed by FK actions) | reversible |
| `pg_drop_table` | `{table}` | `{dropped: "<schema.table>", referencing_foreign_keys: [{table, constraint}], dependent_views: string[]}` | reversible |

`table` is `name` or `schema.name`; the schema defaults to `public`.

- **`pg_query` is read-only three times over.** It connects as its own login role, `mulligan_ro`, with only `SELECT` grants (`PROD_DB_READONLY_URL`), never `SET ROLE`, which the query itself could undo. It runs in a `READ ONLY` transaction with a local `statement_timeout` (`PG_QUERY_TIMEOUT_MS`, default 5000), through a server-side cursor, which allows exactly one statement and fetches at most `max_rows + 1` rows. Rows past `max_rows` are dropped and `truncated` is true. The role can't see `mulligan_trash` or `mulligan_audit`.
- **Soft drop.** `pg_drop_table` renames the table to `<action_id>__<table>` (the action id first, cut to 63 bytes, since Postgres silently truncates longer names), renames its indexes and owned sequences to `<action_id>_i<n>` / `_s<n>` so a second drop of the same name can't clash, and moves it into the `mulligan_trash` schema, which agents can't see. Undo moves everything back under the old names. Trashed tables are purged only after the undo window closes. The gateway's role (`mulligan_gw`) owns the app tables, which `SET SCHEMA` requires.
- **Dependent views** keep working against a trashed table, so the drop revokes `mulligan_ro`'s `SELECT` on them and undo grants it back. The result lists them.
- **Known difference from a real `DROP`:** a move keeps every foreign key that points at the table, and so does a rename. A real `DROP` without `CASCADE` would fail on those FKs, and `DROP ... CASCADE` would remove them. Mulligan does neither: the drop always succeeds, referencing tables keep working against the trashed table, and undo needs no FK repair. The result lists the FKs that still reference the table, and the action log records them. This is deliberate. Don't "fix" it by dropping FKs.
- **Undo order inside one action.** Audit rows are reverted newest first, except that re-inserted rows go parents first, sorted by FK dependency from `pg_constraint`. FKs are never made `DEFERRABLE`, because that would change the customer's schema. Re-inserting into a `GENERATED ALWAYS AS IDENTITY` column uses `OVERRIDING SYSTEM VALUE`.
- **Conflicts.** A column a human changed after the agent is kept and flagged, and the row's other columns are still restored. A deleted row whose primary key a human reused, or a dropped table whose name a human reused, is a conflict: that step is skipped and flagged.

## GitHub

All tools act on `GITHUB_ORG/GITHUB_REPO` only.

| Tool | Input | Result | Label |
|---|---|---|---|
| `github_list_issues` | `{state?: open\|closed\|all = open, labels?: string[], since?: date-time, per_page?: 1..100 = 30, page?: ≥1 = 1}` | `{issues: [{number, title, state, labels, assignees, comments, updated_at}]}` | read_only |
| `github_close_issue` | `{number, state_reason?: completed\|not_planned = completed, comment?: string}` | `{number, state}` | reversible_noisy |
| `github_comment` | `{number, body}` | `{comment_id, url}` | reversible_noisy |
| `github_update_file` | `{path, content: string (UTF-8), message, branch?: string}` | `{path, commit_sha, blob_sha}` | reversible_noisy |

- Closing an issue or commenting notifies watchers, and a file update leaves a commit in history, so undo can't hide that they happened. Undo reopens the issue (and deletes the comment it added), deletes the comment, or writes the old file content back through the contents API with a sha check.
- **Conflicts.** The adapter hashes each issue field the agent changed (`state`, `title`, `body`, `labels`, `assignees`) and each file's blob sha. A field or file whose current hash differs at rewind time is kept and flagged. There is no three-way merge.

## Notion

| Tool | Input | Result | Label |
|---|---|---|---|
| `notion_get_page` | `{page_id?: string = NOTION_PRICING_PAGE_ID}` | `{page_id, title, in_trash, properties: {"<name>": value}, blocks: [{id, type, text, has_children}]}` | read_only |
| `notion_update_page` | `{page_id, properties?: {"<name>": value}, block_updates?: [{block_id, text}], append_blocks?: [{type, text}]}` (at least one of the three) | `{page_id, updated_properties: string[], updated_block_ids: string[], appended_block_ids: string[]}` | reversible |
| `notion_trash_page` | `{page_id}` | `{page_id, in_trash: true}` | reversible |

- Property values are simplified JSON: title and rich text as a string, number, select as a string, multi-select as a string array, checkbox, date as an ISO string, url, email and phone as strings.
- `append_blocks` types: `paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, `callout`, `code`. `block_updates` replaces a block's plain text and keeps its type.
- Undo restores properties and block text, deletes appended blocks and untrashes the page. The block tree is captured before the first write.
- The Notion API can't take a workspace-level page out of the trash, so pages Mulligan protects live under a parent page shared with the integration.
- **Conflicts.** The adapter hashes each property and block the agent wrote. A property or block whose hash changed is kept and flagged, and the rest of the page is restored.

## Held channels (Stream B's `holds/`)

| Tool | Input | Result | Label | Hold |
|---|---|---|---|---|
| `email_send` | `{to: email[] (1..1000), subject, body: string, cc?: email[]}` | `{action_id, status: "held"}` | irreversible | `email_outbox` |
| `payments_charge` | `{customer_id: string, amount_cents: int (1..100000), currency?: string = "usd", description: string}` | `{action_id, status: "held"}` | compensable | `payment_auth` |

- Both are Stream B's adapters in `gateway/mulligan/holds/` (systems `email` and `payments`). A rewind sends an action that hasn't run yet (held or approved) to `HoldManager.cancel`, and one that ran to the adapter's `revert`, like any other system.
- Email waits in an outbox until someone approves it, then sends. A rewind before sending cancels it (`email.cancel_outbox`, `restored`). After sending it is `skipped`, or `compensated` if the adapter sends a correction email (`email.send_correction`).
- A payment is authorized only after approval (`capture_method=manual`) and captured when `hold.release_at` passes the undo window, which must be before `hold.authorization_expires_at` (Stripe's `capture_before`). A rewind before capture cancels the authorization (`payments.cancel_authorization`, `restored`); after capture it refunds (`payments.refund`, `compensated`).

## Mulligan

| Tool | Input | Result | Label |
|---|---|---|---|
| `mulligan_status` | `{}` | `{session_id, agent, action_count, held: [{action_id, tool, summary}], recent: [{action_id, tool, status, summary}]}` | read_only |

## Upstream MCP servers

MCP servers placed behind Mulligan are listed in `UPSTREAM_MCP_SERVERS` (`name=url,…`). Their tools are exposed as `upstream__<server>__<tool>`, with the upstream input schema unchanged and the description prefixed with `[via <server>; held unless read-only]`.

- A server name matches `^[a-z][a-z0-9_-]{0,15}$` and can't contain `__`. A tool whose exposed name would pass 64 characters is skipped with a startup warning, since clients reject longer names.
- Upstream tools have no policy entry and no undo adapter. Every call goes through `Policy.lookup` (which returns `None`) and then `Classifier.classify_unknown`, with `system: upstream` and `upstream_server` set on the action.
- The classifier gets an `UpstreamToolSchema` (`gateway/mulligan/interfaces.py`) whose `name` is the upstream server's **original** tool name (`post_message`), not the exposed `upstream__slack__post_message`. Otherwise every name would start with "upstream", the read-verb rule would never match, and every read would be held. `exposed_name` and `server` are passed separately.
- Only `read_only` goes straight through, and the upstream result is returned to the agent. Any other label, or a classifier error, holds the call (fail closed). The agent gets `{action_id, status: "held"}`; if it's approved, the gateway makes the upstream call and logs the result.
- Upstream calls are logged but can't be undone. A rewind marks them `skipped`, or rejects them if they are still held.

## Field paths and hashes

`Action.after_hashes` maps a field path to the sha256 (lowercase hex) of the value the agent left there. The path grammar is in `action.schema.json` (`Action.after_hashes`):

- `postgres:<schema>.<table>/<pk>/<column>` for one column, `postgres:<schema>.<table>/<pk>` for a row the action inserted or deleted, `postgres:<schema>.<table>` for a dropped table
- `github:issues/<number>/<field>`, `github:comments/<id>`, `github:files/<path>`
- `notion:<page_id>/properties/<name>`, `notion:<page_id>/blocks/<block_id>`, `notion:<page_id>` (trash state)

Each adapter hashes a canonical form of the value, so the same value always gives the same hash. Postgres hashes `mulligan_audit.field_hash(mulligan_audit.canon_row(row) -> column)` in SQL (timestamps in UTC, shortest round-trip floats, ISO intervals, hex bytea); an INSERT hashes every non-generated column, an UPDATE the columns it changed, and a DELETE the whole row as absent.
