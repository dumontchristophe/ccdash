# The dashboard, view by view

What each menu entry, card and column means. For the internals see
[`frontend.md`](frontend.md) and [`backend.md`](backend.md); for the semantics of
cost and of the OTEL variables see [`reference.md`](reference.md).

---

## 0. Common to every view

### The sidebar

Eight entries in three groups. The everyday destinations sit at the top with no
heading.

| Group | Entries |
|---|---|
| — | Summary, Projects, Sessions, Costs |
| Analysis | Context, Tools, Errors & Permissions |
| System | Diagnostics |

A session detail has no menu entry: it opens by clicking a session, or a session
link in a table. The hash is the address — every view is reloadable, bookmarkable,
shareable.

Clicking a sidebar entry **resets the project filter to "all"**; the period and
host filters, set by hand, are kept.

Below 768 px the sidebar becomes a drawer — see [`frontend.md`](frontend.md).

### The filter bar (top right)

| Control | Effect |
|---|---|
| Period | `24 h`, `7 days` (default), `30 days`, `90 days`, `All`. Filters on the timestamp of points and events. |
| Host | Value of the `host` attribute in `OTEL_RESOURCE_ATTRIBUTES`. |
| Project | Same with `project`. `(undefined)` targets rows carrying **no** project. |
| `Auto 10s` | Auto-refresh every 10 s, on/off. |
| `updated Xs ago` | Age of the last successful load. |
| `↻` | Manual reload. |

`Auto 10s`, `updated` and `↻` are on every view. The selectors are dropped where
they could not steer:

- **A session detail** is already bounded to one session, so it keeps none.
- **Diagnostics** keeps only the day selector: `/api/health` bounds its scans by
  the window so the page does not grow with total history, but it stays global
  across hosts and projects — narrowing those would hide the misconfigured one
  you came to find. Pick **All** to read the whole store.

### Bytes or tokens: two quantities not to confuse

| Quantity | Unit | Labels | What it measures |
|---|---|---|---|
| Size of tool results | **bytes** | `Result size`, `Total`, `Median`, `p95`, `Size share` | What a tool hands back, re-injected into the context |
| Model consumption | **tokens** | `Input`, `Output`, `Cache read`, `Cache write`, `Tokens`, `Output tokens` | What the API bills |

Everything a `Read`, `Grep` or `Bash` returns goes back into the conversation and
becomes input tokens on the next turn. The byte figure is the only place that
answers "what is filling my context window": roughly **4 bytes ≈ 1 token**, so
500 KB pulled in is worth ~125K input tokens re-paid every following turn. The two
are never addable.

### Display conventions

- Compacted numbers: `1.2K`, `3.4M`. Bytes: `1.5 KB`. Durations: `45s`,
  `1m 30s`, `2h 05min`.
- Model tags (`Opus`, `Sonnet`, `Haiku`, `Fable`) keep one colour everywhere; any
  other model takes a neutral one.
- **"Est. cost" is not your invoice**: it is the estimate Claude Code computes and
  exports as a metric. See [`reference.md`](reference.md).
- **Project** and **Session** (first 8 characters of the id, clickable) appear only
  in **global** tables, never in a session detail.

---

## 1. Summary

The landing view: the whole period at a glance.

**Four cards**, each compared against the window before it — `▲`/`▼` and a
percentage, or `= 0%` when the change rounds to nothing. The `All` window carries
no comparison.

| Card | Value | Hint line |
|---|---|---|
| Sessions | Distinct sessions having emitted telemetry | Number of user prompts |
| Tool calls | Completed tool calls (`tool_result` events) | Commits and net lines (added − removed) |
| Tokens | Sum of the four token types | `cache miss X%` = cache write over everything read |
| Est. cost | Sum of the cost metric | Cumulative **active** time, not elapsed |

The Tokens total is overwhelmingly cache read — the conversation sent again on
every request — so the hint counts the cache **write** instead.

**Rhythm** — one column per weekday (Monday first), one row per local hour; cell
shade scales with the count against the grid maximum. It counts **telemetry points
received**: a proxy for activity, not working time.

**Projects** — the five most recently active projects, one per row; the pager walks
the rest. Clicking opens Sessions filtered to that project.

**Three breakdowns**, side by side:

- **Models** — API calls answered per family. Counted, not costed: shows that
  Haiku answers often and costs almost nothing.
- **Skills & MCP** — activations per skill, calls per MCP server.
- **Sub-agents** — delegations per agent type.

An empty breakdown usually means `OTEL_LOG_TOOL_DETAILS=1` is unset — those names
are stripped without it, so "empty" reads as "not collected", not "never used".

---

## 2. Projects

The Summary's project block, all rows, unpaginated. Per project: the models seen,
the last activity, and the session / tool / token / cost counts. Clicking opens
Sessions filtered to that project.

A project comes from `OTEL_RESOURCE_ATTRIBUTES="project=..."`; anything without one
falls into `(undefined)`.

---

## 3. Sessions

**Five medians** over every session in the window — active time, prompts, tokens,
producing, cost. They describe the whole period, and the list carries every
session in it.

**Sessions that spent nothing are left out**, here and everywhere a session is
counted. Claude Code reports a session as soon as a process comes up, so a run that
registers its hooks and exits leaves one behind with no tokens and no cost. They
are numerous, and counting them would drag every median down. The figure under the
first card says how many the window held; [Diagnostics](#12-diagnostics) lists them.

**The list**, most recent first, 50 rows per page. Each row: title, project, models,
last activity, host, a compaction count when there is one, and six figures —
duration, prompts, tools, tokens, producing, cost.

**Producing** is the share of a session's *cost* that went into output rather than
into reading the context back. Output means everything the model writes: replies,
but mostly the contents of edited files and run commands — tool arguments are
billed as output. It is not a productivity score; read it against the median card,
not a target.

The **title** comes from the session's first prompt, or from a `/rename` command
(a `renamed` tag marks the second case). Both need `OTEL_LOG_USER_PROMPTS=1`;
without it a session is identified by its project or `(undefined)`.

**Duration is wall clock** (`ended_at - started_at`), not working time — a session
left open overnight reads as a night. Active time is the median figure; the two can
differ by an order of magnitude ([`reference.md`](reference.md) explains why).

---

## 4. A session detail

Reached by clicking a session. Five cards — duration (with active time), prompts,
tool calls (with failures), tool results (bytes read back, and their token worth),
cost — then a tabbed panel and a right-hand column. The token total sits in the
heading of the **Cumulative tokens** panel, not in a card.

Under the title, one line says where the session came from: project, first eight
characters of the id, start time, `renamed` when applicable, the terminal and the
Claude Code version. Terminal and version list every distinct value. A session that
exported neither drops the segment rather than printing empty.

### The `Timeline` tab

One row per event, newest first, with a date header when the day changes. Three
columns: the time, **what kind of row it is**, and **the detail specific to it**.

The left column names the tool a call went through, or the record kind otherwise —
a prompt, an API request, a compaction, a hook fire. A sub-agent's report arrives
through the prompt channel and reads `task-notification`.

The right column carries what the record adds: a compaction's trigger and its
before/after tokens; a permission decision, its source and the modes it switched
between; a skill name; the prompt text; the assistant's answer clipped to 300
characters (`(redacted)` without `OTEL_LOG_ASSISTANT_RESPONSES=1`, the row arriving
either way); an API request's model and duration; an MCP connection's status,
server and transport; a hook's name and fire duration; an API error's status code
and message; an exhausted retry chain's attempts and total wait; an internal
error's name; an at-mention's target; a sub-agent's type and description; a
failure's error type; the refined tool label (`mcp:server/tool`, `skill:name`); the
result size; the Bash command. When a row has nothing to add, the cell is empty.

A hook over **500 ms** is highlighted amber — worth a look, not a failure.

A hook fire takes **one** row. Claude Code exports a start and a completion; the
completion carries everything plus the result and duration, so the start is dropped
at read time (still stored, still counted on Diagnostics).

Any row opens the raw event.

### The `Files` tab

Between `Tools` and `Bash`: one row per file the session **changed**, ranked by
number of changes. Files it only read are not listed.

| Column | Meaning |
|---|---|
| File | The name, directory underneath; the full path is the tooltip |
| Edits | `Edit` calls on that path |
| Writes | `Write` calls (whole-file rewrites) |
| Failures | Edits and writes that reported a failure |
| When | Last time the file was touched |

Sorted on `Edits` by default. Clicking a row lists **the edits and writes made to
that path** — the same calls the two figures count. A `Tool` column names each;
the path sits in the caption.

Clicking one of those calls opens it. An `Edit` or `Write` panel names the file and
shows what was written — the content for a `Write`, the before and after for an
`Edit`. Claude Code clips long values before export, so the panes show a preview; a
line names the clipping ([`reference.md`](reference.md) gives the limits).

### The other tabs

`Tools` and `Other` are the tables of the corresponding global views, restricted to
this session and without the Project and Session columns. `Bash`, `Prompts` and
`Sub-agents` have no global view — §§ 6, 7 and 9 describe them.

### The right-hand column

- **Session** — project, host, models, output style, effort, net lines. Output
  style and effort list every distinct value the session ran under; either empty
  reads `-`.
- **Context** — how big the conversation is **now**, not what it has spent: the
  prompt size of the last main-thread request (fresh input plus cache reads and
  writes). The heading carries that figure, then a curve of prompt size over time.
  The curve starts at the compaction before last, holding the live stretch plus one
  drop; the caption says how many earlier compactions fell off. A **Since last
  compaction** row gives `lowest → current` (**Over the session** when it never
  compacted), and a **Compactions** count opens a panel: each compaction under
  *When*, *Trigger*, *Context measured* and *Reported by Claude Code*. With no API
  request recorded, the box names `OTEL_LOGS_EXPORTER=otlp`.
- **Cumulative tokens** — the total in the heading, then the four types with a
  weighted bar. Each type shows its raw count and, greyed, its share of the weight;
  the bar draws those shares. A caption states the session's cache miss.
- **Request origin** — what the session spent, then one bar per origin: main thread,
  sub-agents, auxiliary calls, compaction, session-title generation. No threshold.
  The caption explains the split — sub-agents and Claude Code's own calls do not run
  on the main loop. A session that cost nothing says so.

A very large session is cut: a banner names which tabs were truncated.

---

## 5. Context

Which sessions ran out of room, and which merely ran long.

Four cards — sessions in the window, auto compactions, manual compactions, and the
largest context seen before any compaction — then one row per session.

The column that matters is **auto compactions**: Claude Code compacts on its own
when the context overflows, so only that says the work outgrew the session. The
table is ranked on it by default, falling back to the peak when nothing compacted.

| Column | Reads |
|---|---|
| Auto compactions | the context overflowed by itself |
| Manual | you asked for it |
| Peak before compacting | the largest context reached before a compaction |
| Max context | the heaviest prompt the session sent |
| Cost | what the session's API requests were billed |
| Tools per prompt | how much a single instruction set off |
| Events | the size of the session's timeline |
| When | the session's last activity |

**Max context** is the largest prompt size over the session's main-thread
requests — the only column describing a session that ran hot without compacting. A
dash under the peak means *never compacted*, not *ran light*: read that row on Max
context instead.

Every column sorts. Rows are **not** clickable; the session identifier is a link,
hidden below 768 px.

---

## 6. Prompts

A tab of the session detail (§ 4), with no menu entry.

One row per prompt and what it set off: when, the text, tool calls, cumulative
result size, failures, compactions triggered, cost, duration. Sorted by date. The
newest turns only — 10000 here, 300 over a whole window — with a note above the
table when the list was cut.

Clicking a row opens the prompt: full text, cost and token breakdown, and the tools
it used. Clicking a tool there lists that tool's calls **within that prompt**.

Without `OTEL_LOG_USER_PROMPTS=1` the text reads `(redacted)`; the counters, drawn
from events, still work.

---

## 7. Sub-agents

A tab of the session detail (§ 4), with no menu entry.

One row per **completed** delegation: when, agent type, model, real tokens consumed,
tool uses, duration.

Clicking a row shows the instructions the agent was given, **clipped by Claude Code
before export** ([`reference.md`](reference.md) gives the limits). The tools the
sub-agent ran internally are not available — those calls share the parent prompt id.

Needs `OTEL_LOG_TOOL_DETAILS=1` for the agent type.

---

## 8. Tools

One row **per tool**, aggregated, not per call.

| Column | Meaning |
|---|---|
| Tool | The name, with a tag for its kind (MCP, Skill, Agent) |
| Calls | Number of calls |
| Failures | Calls that reported a failure |
| Median, p95, Total | Result size in **bytes** |
| Size share | That tool's share of all bytes returned |
| Avg. duration | Mean wall-clock duration of a call |

Clicking a row lists that tool's calls, scoped to the current filters. Below the
table, an inventory of the skills and MCP servers used.

A tool call carries no cost: cost is billed per model request.

---

## 9. Bash

A tab of the session detail (§ 4), with no menu entry.

One row **per call**: the human description, when, the duration, the result size,
the status. Clicking a row shows the full command.

Empty without `OTEL_LOG_TOOL_DETAILS=1` — commands are not sent at all. Enabling it
stores command lines, and whatever they carry, in clear text.

---

## 10. Errors & Permissions

Three tabs, each with a count that says which is worth opening; an empty tab says so
rather than showing an empty table.

- **Failures** — one row per failed call: when, the tool, the error type, and
  **What failed** (the command attempted, falling back to the error message). Every
  shell failure carries the same fixed message, so the command tells repeats apart.
  Clicking shows what was attempted.
- **API errors** — what Anthropic's API refused or gave up on: when, the **Kind**,
  the model, the status code, the message, the attempts and the elapsed time. Kind
  is empty for a plain error, *Retries exhausted* otherwise (*…, no error reported*
  for a chain with no error facing it). Clicking shows the raw event.

  **One row is one incident, not one event.** Claude Code reports an exhausted retry
  chain twice — an `api_error` and an `api_retries_exhausted` on the same request —
  so the second is folded onto the first, which then carries the whole chain's
  totals and is marked *Retries exhausted*. An `api_retries_exhausted` with no error
  facing it still gets its own row.
- **Permissions** — decisions grouped by tool, decision and source: `accept` /
  `reject`, and where the decision came from (a config rule, a one-off answer, a
  standing answer, an abort).

---

## 11. Costs

**Cost over time** — a daily area chart stacked by model family.

**Real weight** — the stacked bar shows a **dimensionless relative weight**
(input ×1, cache read ×0.1, cache write ×1.25, output ×5), each type's share in the
legend. This is where a massive cache read turns out to weigh little, and a small
output volume to weigh a lot.

Those ratios reproduce billing exactly: dividing an `api_request`'s real `cost_usd`
by its weighted unit count gives a whole number of dollars per million units, the
same on every request of a given model. Only the per-model rate is left out.

Four cards: total cost, cache savings in equivalent input tokens, cache write, and
output tokens.

**Cost by project** and **By request origin**, side by side. The origin split reads
`query_origin` (the event's `query_source` without the output-style suffix): the
main thread — under three names, one reading *Main thread (SDK)* for the Claude
Agent SDK — autocomplete, background summaries, session titles, sub-agents,
auxiliary calls and compaction. The caption states the share falling outside the
main thread — the spend you did not explicitly ask for.

---

## 12. Diagnostics

Answers one question: is collection working. It ignores the period, host and project
filters on purpose.

**Six cards**: metric points, events, temporality, database size, delegations,
prompts (and how many carry text).

**Banners** say what collection is doing. All but the first appear when something is
wrong, and name the variable to set:

| Banner | Meaning |
|---|---|
| Prompt content enabled | Nothing wrong: prompt text is arriving. Counts the slash commands and `/rename` seen. |
| Cumulative temporality | Sums are wrong. Set `OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta`. |
| No events received | `OTEL_LOGS_EXPORTER=otlp` is missing; no tool usage will appear. |
| MCP calls without a server name | `OTEL_LOG_TOOL_DETAILS=1` is missing. |
| Prompt content redacted | `OTEL_LOG_USER_PROMPTS=1` is missing (enabling it stores prompts in clear text). |
| Unrecognized events | The Claude Code export format may have changed. |

**Ingestion stream** — per request path: batches, accepted, skipped, last seen. It
records the path of a request that matched no route.

**Metrics received** and **Events received** — what arrived, by name. An `unknown`
line in the events table is flagged.

**Idle sessions** — the ones every other page leaves out for spending nothing:
project, identifier, when they came up, and their metric-point count. The row is not
clickable; the identifier is a link, hidden below 768 px. Here rather than on
[Sessions](#3-sessions) so the exclusion can be checked.

**Hooks** — one row per hook: the event it is bound to, its fires, average and max
duration, errors and blocks. `Errors` means the hook **crashed** and Claude Code
carried on without it. `Blocks` means the hook **refused** the action — one is a
defect, the other the feature working. Clicking shows how it was registered and its
recent fires.

**Ingestion errors** — the journal of what was refused and why, deduplicated.

**Database** — the path of the SQLite file, to query it directly.
