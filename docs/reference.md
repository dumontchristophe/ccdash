# ccdash — Reference

## 1. Cost and token weight

### Displayed cost (`cost` / "Est. cost")

The **Est. cost** card and every `$` figure come from the `claude_code.cost.usage`
OTLP metric. Claude Code computes it from raw token counts and a built-in price
grid; ccdash stores and sums the values as-is.

It is an **estimate**, not your Anthropic invoice: the price grid may be stale,
and it ignores your plan (Pro, Max) and any discount. Hence the label
**"Est. cost"**. The real invoice lives in Anthropic's billing, out of reach.

### Token weight (`WEIGHTS` / "Real weight")

The **Real weight** box on the Costs page shows a **dimensionless relative
weight**, not dollars — raw token counts scaled by `WEIGHTS` (`ccdash/core/aggregates.py`):

```python
WEIGHTS = {"input": 1.0, "cache_read": 0.1, "cache_creation": 1.25, "output": 5.0}
```

| Token type       | Weight | Rationale                                    |
|------------------|--------|----------------------------------------------|
| `input`          | 1.0    | Base reference — one input token = 1 unit    |
| `cache_read`     | 0.1    | A cache read ≈ 10% of a fresh input token    |
| `cache_creation` | 1.25   | Writing a cache entry ≈ 25% more than input  |
| `output`         | 5.0    | An output token ≈ 5× a plain input token     |

These mirror Anthropic's relative pricing, showing **where the token budget
goes** regardless of model or absolute per-token price. They are exact, not
approximate: `cost_usd` divided by weighted unit count gives the same whole
dollars-per-million on every request of a given model — 1 Haiku, 3 Sonnet 4.x,
5 Opus. The only thing left out is the per-model rate, which is what makes the
bar comparable across models.

Types are snake_case above storage; the OTLP wire spells two of them `cacheRead`
and `cacheCreation`. `TOKEN_TYPES` (`ccdash/core/aggregates.py`) converts them as a
payload is built — the only place it happens. The exception is
`/api/projects`, `/api/sessions` and `/api/session`, which pivot on the stored
wire spelling in SQL; a rename must follow there too.

`WEIGHTS` is editable and drives: the relative bars on Costs and session detail,
the `producing` column and **Median producing** card on Sessions, and the
cache-savings card (a cache read counts as `1 - cache_read` of a fresh input
token). It never touches a `$` figure — those always come from
`claude_code.cost.usage`.

### Cache miss

Computed on **raw** counts, a ratio of flows:

```
cache miss = cache_creation / (cache_read + cache_creation + input)
```

The cache write is the context that had to be sent fresh; everything else was
already there. The intuitive `cache_read / (cache_read + input)` is unusable —
fresh input is negligible against re-reading, so it saturates near 100% whatever
the session did. Shown as miss rather than hit for the same reason: sessions an
order of magnitude apart on the miss are a few points apart on the hit.

---

## 2. OTEL environment variables

Set these in `~/.claude/settings.json` under `"env"` (global). For
per-repository attribution, put `OTEL_RESOURCE_ATTRIBUTES` in the repo's
`.claude/settings.json`. The values below match README.md's block.

| Variable | Required value | Effect | Privacy |
|---|---|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `"1"` | Master switch — off, nothing is exported and the DB stays empty. | None by itself. |
| `OTEL_METRICS_EXPORTER` | `"otlp"` | KPI cards, cost charts, token counters, Rhythm grid. | None — metrics carry no content. |
| `OTEL_LOGS_EXPORTER` | `"otlp"` | Tool calls, session events, sub-agents, permission decisions. Off, Events tables are empty and Diagnostics warns. | Low — content gated behind separate flags. |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `"http/json"` | Any data at all — ccdash rejects protobuf with HTTP 415. | None. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `"http://127.0.0.1:4318"` | Connectivity — the destination address. | None. |
| `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` | `"delta"` | Correct sums. `"cumulative"` makes ccdash sum running totals and inflate everything; Diagnostics shows a red warning. | None. |
| `OTEL_METRICS_INCLUDE_ACCOUNT_UUID` | `"false"` | No view uses the account UUID; ccdash strips PII anyway, but `"false"` avoids sending it. | `"false"` reduces PII in transit. |
| `OTEL_LOG_TOOL_DETAILS` | `"1"` | Full Bash command; sub-agent type/description; MCP server names. Diagnostics notes MCP calls missing a name. | Bash commands may hold paths, args, secrets. |
| `OTEL_LOG_USER_PROMPTS` | `"0"` (default) | Session titles and the Prompts tab text. Off, sessions show by ID and prompts read "(redacted)". | **High** — enabling stores every prompt in clear text in `ccdash.db`. |
| `OTEL_LOG_ASSISTANT_RESPONSES` | `"0"` (default) | Assistant answer on the timeline (clipped to 300 chars) and in the event inspector. Unset, it follows `OTEL_LOG_USER_PROMPTS`. Text capped at 60 000 chars or `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH`. | **High** — enabling stores every answer in clear text. |
| `OTEL_RESOURCE_ATTRIBUTES` | `"host=…,project=…"` | The `host` and `project` filters, project breakdown cards, per-project cost. ccdash reads exactly `host` and `project`; other spellings fall into "(undefined)". | None — labels you define. |

`OTEL_METRICS_INCLUDE_VERSION` is not needed: every log record already carries
`service.version`, which is what the line under a session title reads.

## 3. Durations and hook overhead

### Two notions of session length

- **Duration** (`ended_at - started_at`) — wall clock between the first and last
  telemetry point. Shown per Sessions row and as the session-detail headline. It
  counts idle time, so a session left open overnight reads as a night.
- **Active time** (`claude_code.active_time.total`) — what Claude Code counted as
  actually working. Sub-label of the session Duration card and the Overview cost
  card, and the figure the Sessions medians report.

Those medians cover every session in the window, and the list now carries them
all rather than the 300 most recent. A session with no `active_time` counts as
zero.

### Where the spend comes from

Two readings of the same spend, from two sources:

- **Costs → By request origin** splits by `query_origin` — the `query_source`
  attribute with the `:outputStyle:<name>` suffix stripped, so one origin is one
  row whatever style was in force. The main thread answers to three names:
  `repl_main_thread` (CLI), `main` (builds before mid-2026), `sdk` (Claude Agent
  SDK, labelled "Main thread (SDK)"). Beside it: `prompt_suggestion`,
  `away_summary`, `generate_session_title`, `auxiliary`, `compact`, and
  `subagent` (or `agent:…`). Surfaces cost you didn't ask for; the caption gives
  the share falling outside the main thread.
- **Session detail → Request origin** asks the same of one session, reading the
  coarser `query_source` on the metric points rather than the event blob. Every
  origin is drawn with no threshold.

### Hook overhead (Diagnostics → Hooks)

The **Hooks** table aggregates `hook_execution_complete` events by hook name:
fire count, average and max `total_duration_ms`, and the **summed** error and
block counters (a fire reporting two errors adds two — neither column is a fire
count). This is time your hooks add on top of Claude Code. Generic over the hook
event, so `PreToolUse`, `PostToolUse`, `UserPromptSubmit` all appear. Empty
means no hook has fired.

The aggregate says which hook costs; the **session timeline** says which fire
did, carrying each `total_duration_ms`, highlighted above 500 ms.

`Errors` (`num_non_blocking_error`) means the hook **crashed** — a non-zero exit
that isn't 2 — and Claude Code carried on; nothing else surfaces a silently
broken hook. `Blocks` (`num_blocking`) means the hook **refused** the action, via
exit code 2 or a `deny` decision. One is a defect, the other the feature working.

## 4. Click-through detail (what is and isn't available)

Rows open a detail modal in **Tools**, **Bash**, **Prompts**, **Sub-agents**,
the **Failures** table (Other), the **Files** tab of a session, and the **Hooks**
table (Diagnostics). The telemetry bounds each:

- **Tools** — the calls behind one tool, scoped to how the modal was opened (one
  turn in a prompt modal, one session in a session detail, current filters
  otherwise).
- **Files** — the `Edit`/`Write` calls to one path, with the content written.
  That content is **clipped upstream** (see below).
- **Prompts** — full prompt text, its cost and token breakdown, and the tools
  the turn used. Text needs `OTEL_LOG_USER_PROMPTS=1`; counters don't.
- **Bash** — full command, description, duration, output size, accept/reject.
  **No per-command cost**: cost is billed per model request, not per tool call.
- **Failures** — error type, message, and the command/parameters attempted.
- **Sub-agents** — the instructions (from the spawning Task/Agent call), plus
  real tokens / tool-use count / duration / model, the `effort` its requests ran
  at, the `isolation` asked for, and whether it ran in the background
  (`is_async`). The **list of internal tools it ran is not available**: those
  calls share the parent `prompt.id` and can't be separated from the main
  thread. Instructions are **clipped upstream**.
- **Hooks** — the matcher/source it was registered with and its 200 most recent
  fires (duration, session, project, errors, blocks). The **hook's output and
  command are never in the telemetry**: the `hook_definitions` attribute holding
  the command line is gated on an internal beta-tracing path a normal session
  never hits. Hooks sharing one matcher report under the same name with the total
  duration; the fire count distinguishes them.

### The `tool_input` ceiling

Claude Code sanitises `tool_input` before export, so long values arrive already
clipped and nothing downstream can recover them:

| Value | Limit | What arrives |
|---|---|---|
| String | > 512 chars | first **128** chars + `…[N chars]` (`N` = original length) |
| Array | > 20 items | first 20 + `…[N items]` |
| Object | > 20 keys | first 20 + `…: N keys` |
| Nesting | depth 2 | `<nested>` |
| Whole serialised attribute | 4096 chars | truncated + `…[truncated]` |

This hits **sub-agent instructions** hardest: a Task prompt of a few thousand
chars shows the first 128 with the marker stating how much was dropped. Read
`…[1264 chars]` as "1264 written, 128 exported". The Edit/Write panel reads the
same marker; a `tool_input` past 4096 chars carries nothing, and the panel says
so.

**Not** affected, taking a different export path: the user prompt text (the
`user_prompt` event, gated only by `OTEL_LOG_USER_PROMPTS`) and a Bash
`full_command`, both exported whole.
