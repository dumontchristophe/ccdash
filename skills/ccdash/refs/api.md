# ccdash API — the parts the JSON won't tell you

Read-only JSON over HTTP, no auth, no pagination, `GET` everything. The response
keys are legible, so this file caches only what reading the payload cannot give
you: the filters, the routes, and the semantics the key names hide.

## Filters

`days`, `host`, `project` apply to the *filtered* routes below; they AND together.

- `days=N` — a rolling window ending **now**. Absent means the last **7 days**,
  so a question about totals must pass `days=0` — the only way to ask for all of
  history. There is no `from`/`to`; the window always ends now. An explicit date
  range gets the covering `days=N` and a line saying so.
- `project=` / `host=` — one value, taken from `/api/filters`. A name not in that
  list returns an **empty** window, not an error — resolve it first. The literal
  `(undefined)` selects rows whose project is NULL.

The detail routes (`/api/session`, `/api/prompt`, `/api/event`, `/api/subagent`)
take an `id` and ignore the filters — the id already names the scope.

## Routes

| Route | Params | Purpose |
|---|---|---|
| `/api/health` | `days` | ingest diagnostics; **read `temporality` first** |
| `/api/filters` | — | the `hosts` and `projects` lists to scope against |
| `/api/overview` | *filtered* | the whole shape of a window in one call; carries `prev` |
| `/api/projects` | *filtered* | the per-project array of `/api/overview`, alone |
| `/api/costs` | *filtered* | cost series, per-model weights, origins split |
| `/api/context` | *filtered* | context pressure, one row per session |
| `/api/sessions` | *filtered* | one row per spending session, newest first, every spender in the window |
| `/api/session` | `id` | everything about one session; the heaviest payload |
| `/api/prompt` | `id` | one user turn: hook timings, per-tool breakdown |
| `/api/subagent` | `id` (event id) | one sub-agent's task and instructions |
| `/api/analysis` | *filtered* | window-wide bash / errors / subagents / prompts lists |
| `/api/calls` | *filtered*, `label`, `session`, `prompt`, `file` | raw tool calls, 200 rows |
| `/api/event` | `id` | one raw event row |
| `/api/hook` | `name` | one hook's registrations and 200 recent fires |

## Reading conventions

- Timestamps (`ts`, `started_at`, `ended_at`, `last`) are Unix seconds, UTC.
- Durations are milliseconds (`duration_ms`, `avg_duration_ms`, `p95`) except
  `active_seconds` and `duration_s`.
- Sizes (`bytes`, `result_bytes`, `total_bytes`, `median_bytes`) are bytes of
  tool output.
- `success_bool` is a typed **boolean** — the serve boundary normalises the OTEL
  string `"true"`/`"false"`.
- Errors: `/api/session` answers **404** `{"error":"not found"}` for an unknown
  id; `/api/event`/`/api/subagent`/`/api/prompt` answer **200** with an `error`
  key — check for it. A non-numeric id is **400**. Any host the server was not
  started under is **403** `{"error":"host"}` — call it at its own address.

## Semantics the key names hide

- **`/api/sessions` `median`** holds **medians** over the window, not sums — the
  typical session. `idle` counts sessions that spent nothing (excluded from the
  rows). `output_weight_pct` is the % of billing weight spent producing rather
  than re-reading context; low means a session that mostly read itself back.
- **`/api/context`**: `max_context` is the session's high-water prompt (the "ran
  hot" column); `pre_compaction_peak` is the pre-token size at a **compaction**,
  `0` when it never compacted (so `0` ≠ "never grew"); `tools_per_prompt` is
  tool-calls per prompt. Top-level `auto_compactions`/`manual_compactions` are
  the window totals; rows sort by `auto_comp` then `pre_compaction_peak`.
- **`/api/overview`**: `prev` is the same four KPIs over the previous window of
  equal length — the trend, server-side, no second request. `rhythm` is a 7×24
  matrix, weekday-major (0 = Monday). `commits`/`loc_*` are Claude Code's own
  counters — what Claude changed, not what was committed by hand.
- **`/api/costs`**: `weights` reproduces billing (output ×5, cache_read ×0.1);
  `weighted` applies them — raw counts vs weighted separates "read a lot of
  cache" from "cost a lot". `origins` splits by real request origin; sum the
  rows where `is_main_thread` is false for spend outside the main loop rather
  than matching names yourself — a large non-main share is overhead, not work.
- **`/api/session`**: `events` is the timeline, oldest first, 10000 max; on a
  `compaction` row `trigger_kind` is `auto`/`manual` and `pre_tokens`/
  `post_tokens` bracket it. `files` lists only **changed** paths (Edit/Write),
  and exists nowhere else — a period-wide file answer aggregates these or reads
  `/api/calls?label=Edit`. `sources` splits by origin; a large `auxiliary` row
  is Claude Code's own overhead.

## `/api/health` — the config check

Takes `days` only (`host`/`project` ignored — diagnostics stay global). Beyond
`temporality`: `prompts_total` vs `prompts_text` dates any `OTEL_LOG_USER_PROMPTS`
gap; `masked_mcp` counts MCP calls whose server name never arrived; `unknown`
counts events the ingester did not recognise (a rising number means an exporter
newer than this ingester); `idle` lists spendless sessions; `hooks` is the
source for hook cost, `block` counting denied calls. `db_size` and `notes`
describe the whole store regardless of the window.
