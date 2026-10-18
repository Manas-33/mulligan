# SDK notes

Check each SDK against its current docs before you use it. Record the pinned version, the docs link, the date you checked, and any gotchas. Add entries below; don't rewrite old ones.

Template:

```
## <SDK> — <pinned version>
Docs: <url>  ·  Checked: YYYY-MM-DD by <A|B>
- Gotcha / decision
```

## Docs index
- Official MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk
- Claude Code MCP: https://code.claude.com/docs/en/mcp · hooks: https://code.claude.com/docs/en/hooks
- Devin custom MCP: https://docs.devin.ai/work-with-devin/mcp
- Fetch ASI:One agent: https://uagents.fetch.ai/docs/examples/asi-1
- Photon spectrum-ts: https://photon.codes/docs/spectrum-ts/introduction
- Sentry MCP monitoring (Python): https://docs.sentry.io/platforms/python/tracing/instrumentation/custom-instrumentation/mcp-module/

## Entries

_(none yet. Phase 0 fills these in)_

## Notion API `2026-03-11` + notion-client (Python) `3.1.0`
Docs: https://developers.notion.com/reference/versioning · https://developers.notion.com/docs/upgrade-guide-2026-03-11 · https://github.com/ramnes/notion-sdk-py · Checked: 2026-10-17 by A
Spike 0.4 (Notion restore): **PASS** on both criteria. `spikes/04-notion-restore/spike.py run` restores the page to the original normalized hash, and with a teammate edit it keeps and flags that one block and matches the original everywhere else.
- **Versions.** `2026-03-11` is the current API version. notion-client 3.1.0 (Python, community-maintained; Notion's only official SDK is JS `@notionhq/client` 5.27.1) still defaults to `2025-09-03`, so pass `Client(notion_version="2026-03-11")`. 3.1.0 already lets `position` and `in_trash` through its body filters.
- **Trash field.** `2026-03-11` renamed `archived` to `in_trash` in requests and responses for pages, blocks, databases and data sources. Pages also return a separate `is_archived` field, which isn't the trash flag. `blocks.delete` sets `in_trash: true`, and `blocks.update(id, in_trash=False)` brings the block back with the same id and children.
- **Workspace-level pages can be trashed but never untrashed by the API** ("Unarchiving workspace level pages not supported by the API"). Every page Mulligan must be able to rewind sits under a parent page shared with the integration. The demo pricing doc lives under "Acme Widgets", and seed/reset must keep it there. Hold or block `notion_trash_page` on any workspace-level page.
- While a page is trashed, `GET /blocks/{page}/children` returns 404 `object_not_found`, the page drops out of search, and block edits inside it fail with `validation_error`. So a teammate can't fix a trashed doc. Snapshot before trashing, and untrash first on rewind (newest-first order does this anyway). Read any child order only after that untrash.
- **Ordering.** Untrash always re-adds the block at the **end** of its parent, not where it was, and there's no move-block endpoint. To put blocks back with their original ids, trash every block from the first out-of-place one onward, then untrash them in the right order: 2 requests per block in that tail. The cheaper alternative is to recreate the block with `position: {type: "after_block", after_block: {id}}` (or `{type: "start"}`), but that gives it a new id. Use the previous sibling recorded at delete time as the anchor. Rewinding newest first guarantees that sibling is already back in place.
- `2026-03-11` replaced the flat `after` param on append with `position` (`after_block` / `start` / `end`). With `position`, the append response's `results` holds the new block **followed by every later sibling**, so take `results[0]` and don't assume `len == 1`.
- **Ids.** The API returns dashed UUIDs (e.g. in `block.parent.page_id`). Normalize every id to the dashed form before comparing. `NOTION_PRICING_PAGE_ID` in `.env` is the page URL: take the last 32-hex run.
- **Normalizing for hashes.** Drop `plain_text`, `href`, nulls, ids and timestamps, and keep `annotations`. Compare against what the API echoes back, not what you sent: responses fill in the default annotations.
- **Conflicts.** Hash each block without its children, plus `in_trash`, and each property as `{type, value}`. Record the hash from the write's response. At rewind, a block or property whose current hash differs was changed by a human: keep it and flag it.
- **Recreating blocks.** `table`, `column_list`, `column` and `tab` must send their children inline in the create call. Other blocks with children can be created first, then have their children appended. `table_width` can't be changed after creation, so strip it from update payloads. An original `synced_block` must send `synced_from: null` explicitly.
- These round-trip through read → recreate: heading_1–4, paragraph, bulleted/numbered list items, to_do, toggle, quote, callout, code, equation, divider, table + table_row, table_of_contents, breadcrumb, bookmark, embed, external image, link_to_page, column_list, synced_block, tab, and date and page mentions.
- These can't be recreated: `child_page`/`child_database` (use `pages.create`), `link_preview`, `button`, `template` ("Cannot create a template block"), `meeting_notes` (renamed from `transcription`, "Cannot create a meeting_notes block"), plus `ai_block`, `custom_block` and `unsupported`. Notion-hosted files (`type: "file"`) return 1-hour signed URLs and need re-uploading through the File Upload API. User mentions must be trimmed to `{id}` before re-sending, because the API rejects the echoed `name`/`type`/`bot` fields.
- **Rate limits.** 180 requests/min per connection, about 3/s (600/min on Business/Enterprise), plus a per-workspace cap. A 429 returns `rate_limited` with `Retry-After` in seconds. Retry 529 `service_overload` the same way, but never retry `public_api_request_blocked`. notion-client 3.1.0 retries 429s and 5xx itself with backoff, but not read timeouts: it raises `RequestTimeoutError` straight away, so the adapter must retry reads itself. A full rogue → rewind → verify cycle on the 12-block page takes about 55 requests, and the whole spike run took 165 requests in 55–100 s with no 429s.
- **Size limits.** 100 blocks per children array, 1,000 block elements per payload, 500 KB per payload, 2,000 characters per `text.content`, 1,000 per equation, 2,000 per URL. One append request takes at most 2 levels of nesting.
