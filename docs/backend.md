# Backend — `ccdash/`

The `ccdash/` package, Python standard library only. No dependency, no outbound
call, nothing to build — the one build step is the stylesheet, which the backend
only reads from disk. It receives OTLP over HTTP, stores it in SQLite, and serves
both the JSON API and the dashboard's static files.

## Layout

The package is flat — three folders, everything else a top-level module:

```
ccdash/
  __main__.py     `python3 -m ccdash`, delegates to server.main
  server.py       PAGE, ASSETS, Handler, main
  api.py          the routing table
  ingest.py       the write path
  pages/          the endpoints, one file per domain
    overview.py  costs.py  sessions.py  details.py  health.py  analysis.py
  core/           the shared read path
    aggregates.py  request.py  store.py
  web/            the served frontend: index.html, assets/
```

Imports only run **downward**, and nothing points back at `server.py`:

```
server → api → pages/* → core.aggregates → core.request → core.store
```

`store.py` is the floor every module reaches and imports nothing above it, so no
read or write path pulls in the entrypoint (why, under `python3 -m ccdash`, that
matters is in *Storage* below). Imports are package-relative and name the module
(`from ..core import store`), never its symbols — `overview.py` in `pages/`
reaches `store` as `from ..core import store`, `analysis` as `from . import
analysis`.

| Module | What lives there |
|---|---|
| `store.py` | The private connection `_db` and its `_db_lock`, `TABLES`, `INDEXES`, `db_init` / `db_close`, the four query helpers `query` / `query_row` / `query_dicts` / `query_value`, the `write` context manager, and the three decoders both paths need: `as_int`, `as_float`, `tool_input` |
| `ingest.py` | The write path: `anyvalue`, `kvlist`, `nano_to_s`, `make_label`, `ingest_metrics`, `ingest_logs`, `log_ingest`, `INGESTERS`, `DROP_ATTRS`, plus the transport limits and `inflate` / `read_chunked` |
| `request.py` | What a request is read through: `Scope`, `Filters`, `one_param` / `int_param`, and the two refusals `NotFoundError` / `BadRequestError` with the bodies `NOT_FOUND` / `BAD_REQUEST` |
| `aggregates.py` | The vocabulary the read path shares: `TOKEN_TYPES`, `WEIGHTS`, `KINDS`, `MAIN_THREAD_ORIGINS`, the `SPENT_SESSIONS` / `IDLE_SESSIONS` and `SESSION_TOTALS` fragments, the `HOOK_*` expressions, `short_model`, `attrs_of`, `tokens_by_type`, `weighted_tokens`, `capped`, and the query renderers `scoped` / `windowed` with the `SCOPE_MARK` they fill |
| `analysis.py` | The analyses a scope is read through — `tool_stats`, `file_stats`, `bash_calls`, `errors_calls`, `provider_errors`, `decisions_stats`, `delegation_types`, `subagents_stats`, `prompt_stats`, `inventory_stats`, `source_breakdown` — plus `api_analysis`, `api_calls` and `ANALYSIS_CAPS` |
| `sessions.py` | A session listed and opened: `session_titles`, `session_models`, `session_figures`, `api_sessions`, `api_session`, `api_context`, and `SESSION_MAX_ROWS` |
| `costs.py` | What was spent: `api_projects` and `api_costs` |
| `overview.py` | The landing page: `api_filters`, `headline_figures`, `api_overview` |
| `details.py` | One record opened: `api_event`, `api_subagent`, `api_prompt` |
| `health.py` | Diagnostics: `hook_stats`, `api_hook`, `api_health`. Every `events`/`metric_points` counter is bound by the day window (`scope(window_only=True)`); the ingestion journal and the database path stay global, and host/project never narrow the page |
| `api.py` | The `API_ROUTES` table alone: one entry per route, pointing at the module that owns it |
| `server.py` | The server body: `PAGE`, `ASSET_FILES`, `ASSETS`, `WEB_DIR`, `Handler`, `main`. `__main__.py` is a two-line delegate so `python3 -m ccdash` runs it while tests import `server` directly |

The image `COPY`s the whole `ccdash/` package, so a new module ships with no
`Dockerfile` edit; a test asserts the `COPY` names the package, not individual
files.

`tool_input` sits in `store.py`, not beside the endpoint that ships it: the
ingester reads the field from the attributes it holds, `api_event` reads it back
from the stored JSON. Anywhere else it would be the one arrow from `ingest` to
the read path — a cycle.

`store.py` exists so no read or write module imports the entrypoint. Under
`python3 -m ccdash`, `__main__` imports `server`; an arrow back at it from a page
would load a second copy under `__main__` — two connections, and the endpoints
would read the one still at `None`.

`db_path` and `verbose` are reassigned at runtime by `main` (lowercase says so).
Every module does `from ..core import store` and reads `store.db_path`, never
`from store import db_path`, which copies the binding at import time and freezes
it empty. The
connection is exempt: it is private, reached only through a function name, which
is never rebound.

## Storage

Four tables, created by `db_init` from `TABLES`, which is the whole schema and
the single source of truth: every column the code writes is declared there, and
a fresh database is created complete from it. There is no migration machinery —
ccdash is fresh-only. An existing base is brought to a new schema by hand (a
one-off `ALTER TABLE ADD COLUMN` plus a backfill from `attrs`, run once against
the file), never at boot.

`db_init` restricts file modes: the database and its `-wal`/`-shm` siblings to
`600` (the three hold the same rows — prompt text, shell commands, absolute
paths), the directory to `700` when this start created it. A pre-existing
directory belongs to whoever made it — `--db` names a project checkout as
readily as `~/.ccdash`. `_restrict` only clears bits: a mode widened on purpose
keeps what it grants, one already tight is left alone. A `chmod` the filesystem
refuses (bind mount, Windows) warns on stderr and the server starts anyway.

- **`metric_points`** — one row per OTLP data point. Carries the columns the API
  filters or groups on (`session_id`, `host`, `project`, `model`,
  `query_source`, `attr_type`, …). Its `attrs` is written NULL: no route opens a
  data point by id, so a backfill from `attrs` is only possible on `events`.
- **`events`** — one row per log record: tool results, API requests, prompts,
  permission decisions, compactions, sub-agent completions, hook fires. Same
  principle, more promoted columns. `label` names the tool a call went through,
  refined by `make_label` for the generic ones (`mcp:server/tool`, `skill:name`,
  `agent:type`). A record that is not a tool call has no label — `make_label`
  returns `None`, and a placeholder would be indexed and grouped as a tool.
- **`event_attrs`** — the raw attribute blob, one row to one event (#180). It
  is half the weight of `events` and only the inspector opens it, one row at a
  time, so it sits out of the table every aggregate scans. `_insert_rows` writes
  it in the same transaction as the events batch, keyed by the id each row got
  (the contiguous run ending at `last_insert_rowid()`, since the write lock lets
  nothing interleave); a `hook_execution_start`, whose blob `_raw_attrs` drops,
  gets no row. The three inspector reads (`api_event`, `api_subagent`,
  `_spawn_instructions`) `JOIN` it back. `events.attrs` stays in the schema —
  the invariant forbids dropping a column — but is written NULL from now on; a
  base predating the sibling is moved over by a one-off hand rebuild, not at
  boot. The blob is written zlib-compressed (level 6, `_raw_attrs`, #181): the
  column is declared `BLOB`, and `store.attrs_json` is the one decoder every read
  goes through — it inflates a compressed blob, passes a pre-compression text row
  through unchanged, and reads a NULL as `{}`, so the two forms coexist until a
  rebuild recompresses them.
- **`ingest_log`** — the ingestion journal, read by the Diagnostics page: what
  was accepted, skipped, and why. A failure is journalled under its exception
  class name, never its `repr`, because the note leaves the server over
  `/api/health` and a message carries the paths and payload that produced it. The
  traceback goes to stderr.

One module-level connection `store._db`, guarded by `store._db_lock`. The server
is a `ThreadingHTTPServer`, so the lock serializes every database access. Neither
name leaves the module: reads go through the `query*` helpers, writes through
`store.write()` — the context manager that takes the lock, yields the
connection, commits on clean exit and rolls back otherwise (without which a
raised statement leaves its transaction open for the next batch to join).
Pairing the connection with its lock is why the two globals are private, while
`db_path` and `verbose` are public.

WAL is on and `cache_size` is raised to 64 MB: every aggregate walks an index
into the main table, and the 2 MB default drops those pages between two queries
of the same page. Moving the raw `attrs` out to `event_attrs` (#180) roughly
halved the bytes each of those scans touches. `synchronous=NORMAL` is the paired
trade-off.

## Ingestion

`do_POST` dispatches on the path suffix — suffix and not equality, because an
exporter can be configured with a prefixed base URL:

| Suffix | Handler |
|---|---|
| `/v1/metrics` | `ingest_metrics` — `resourceMetrics → scopeMetrics → metrics → dataPoints` |
| `/v1/logs` | `ingest_logs` — `resourceLogs → scopeLogs → logRecords` |
| `/v1/traces` | accepted and journalled, stored nowhere |

Refused before anything is read:

- a request carrying an **`Origin`** header — an exporter never sends one, a
  browser always does, so its presence means a web page is posting (403);
- **protobuf** — only `http/json` is accepted (415);
- a body whose announced length is over `MAX_BODY` (413);
- a `Content-Length` that is not a number, or is negative (400).

A `Transfer-Encoding: chunked` body is reassembled by `read_chunked()`, which
enforces `MAX_BODY` as it reads since no header states the size up front. `gzip`
and `deflate` bodies are then decoded through `inflate()`, which caps the
decompressed size. `DROP_ATTRS` strips the six `user.*` / `organization.*`
attributes before storage.

## API

`API_ROUTES` maps a path to a lambda with a uniform `(params, filters)`
signature. `filters` is the `Filters` dataclass, carrying the decoded `days`,
`host` and `project`; each lambda reads only what its `api_*` needs.

| Route | Answers |
|---|---|
| `/api/overview` | headline figures, previous-window comparison, tokens, model calls, weekly rhythm, project list, three breakdowns |
| `/api/projects` | one row per project |
| `/api/sessions` | `sessions`, every spending session in the window, and `median`, the medians over that same set |
| `/api/session?id=` | one session: header, events, and the same analyses as the global pages, capped at `SESSION_MAX_ROWS` with whatever it cut named in `truncated` |
| `/api/analysis` | tools, bash, skills, MCP, decisions, errors, api_errors, prompts, sub-agents |
| `/api/costs` | daily series, per model, per project, per request origin |
| `/api/context` | one row per session: auto and manual compactions, `pre_compaction_peak`, `max_context`, cost, tools, prompts and `tools_per_prompt`; plus the window totals `auto_compactions`, `manual_compactions` and `pre_compaction_peak` |
| `/api/calls` | the calls behind one label or one file |
| `/api/event?id=` · `/api/subagent?id=` · `/api/prompt?id=` · `/api/hook?name=` | detail for one record |
| `/api/filters` | the values offered by the host and project dropdowns |
| `/api/health` | diagnostics: ingestion journal, event and metric names, counters |
| `/health` | liveness only — `{"ok": true}` |

`filters.scope()` renders the `days` window plus `host` and `project` into a
`Scope`: the SQL clause and its values, as one object. Every aggregate takes a
`Scope` — one a route built from its filters, or one built around a single
session or prompt (`Scope(" AND session_id=?", (session_id,))`), which is how a
session detail runs the global analyses unchanged. `Scope.narrow()` appends a
condition and returns a new scope, so a drill-down cannot alter the window it
came from. There is no empty default: a query the window does not bound is
`Scope.UNBOUNDED`, named, never an omitted argument.

A `Scope` is not composed by hand. `aggregates.scoped(select, population,
scope, …)` assembles the `SELECT`, and `aggregates.windowed(sql, scope)` fills a
ready template; both replace every `{scope}` marker in the SQL with the clause
and repeat the args once per marker — the alignment a nested subquery used to
spell `scope.args * 2`. A template with no marker raises: a windowed helper with
nowhere to put the window would be the silent scan this seam removes, and a
query that means the whole store with no window calls `store.query*` directly.
`test_build.py` also fails any read module that reaches for `scope.clause` or
`scope.args` itself, so forgetting the window is a test failure, not a
full-table scan.

`filters.scope(previous=True)` slides the window back by its own length — how
each headline figure carries its change. On the `All` window (`days=0`) it
produces no clause, so that window carries no comparison.

Besides those three, some routes read a parameter of their own:

| Route | Parameters |
|---|---|
| `/api/session` · `/api/event` · `/api/subagent` · `/api/prompt` | `id=` |
| `/api/hook` | `name=` |
| `/api/calls` | `label=`, `file=`, and `session=` or `prompt=` to scope the list to one of them instead of the global window. Neither `label` nor `file` answers `[]`: the most recent calls of anything is not a drill-down |

`/api/event` and `/api/subagent` read `id` through `int_param`; `/api/session`
and `/api/prompt` take a string id through `one_param`.

`max_context` on `/api/context` is a **maximum**, not a mean: the largest prompt
the session sent on its main thread.

An `id=` naming no record is not a server failure. `/api/session` is the one
route that *raises* `NotFoundError` — its head is a set of aggregates, which come
back as a row of `NULL`s an unknown id cannot be told from a dead session — and
the handler answers `404` with `{"error": NOT_FOUND}`. The other detail routes
select a row and return `{"error": NOT_FOUND}` with a `200`.

An id that is not a number is not a failure either: `int_param` raises
`BadRequestError`, answered `400` with `{"error": BAD_REQUEST}`. Parsing it
tolerantly would answer an empty payload, which reads as "no such record" for an
id the caller simply misspelled.

The catch-all turns everything else into a `500` carrying `SERVER_ERROR` alone,
with the exception written to stderr. The body is what an unauthenticated reader
gets: module names, file paths and SQL stay server-side.

### No response cache

Every `API_ROUTES` answer is computed on the request: the handler runs its
queries and `encode_body` serializes the result, with nothing held between
requests. A server-side cache once sat here, keyed by `(path, query string)` and
expiring on a TTL; it was removed, since for a single local reader the client
already serves a revisited view from its own `Map` and the poll interval
outran any useful TTL. A reading is always current with the database.

`Cache-Control: no-store` on every response tells the *browser* not to re-serve
an API body on a tab switch.

`api_errors` is the one section whose rows are not raw events: `provider_errors`
folds an `api_retries_exhausted` onto the `api_error` it doubles — same session,
same `prompt.id`, within a second of each other (a second of slack because `ts`
is an epoch second and the two records are emitted milliseconds apart, so an
incident can straddle a boundary) — and ships one row per incident. Folding is
one-to-one, enforced by a link table inside the query; whatever is left over (an
exhausted chain with no error, an error nothing exhausted) still gets its row.

`api_session` is also the one place a read is not faithful to the table: its
timeline drops `hook_execution_start`, whose every attribute is repeated by the
`hook_execution_complete` of the same fire. Dropped in SQL, so `SESSION_MAX_ROWS`
applies to what is shown. Nothing else filters it — an id names its own scope.

`api_health` reads the day window like the aggregate endpoints, so its scans do
not grow with total history; it ignores host and project, and its ingestion
journal and database size stay global.

Every per-row listing is capped; the aggregates beside them (`tool_stats`,
`decisions_stats`, `inventory_stats`, the idle sessions of `api_health`) cover
the whole window whatever its length.

| List | Ceiling |
|---|---|
| `bash_calls`, `subagents_stats`, `prompt_stats` | `ANALYSIS_CAPS` — 300 |
| `errors_calls`, `provider_errors` | `ANALYSIS_CAPS` — 200 |
| the seven lists of `api_session` | `SESSION_MAX_ROWS`, 10000 |
| `file_stats` | 500 |
| `api_calls` | `CALLS_MAX_ROWS`, 200 |
| `api_hook` fires | `HOOK_MAX_FIRES`, 200 |
| `api_health` ingestion notes | 10 |
| a session title, in characters | `TITLE_MAX_CHARS`, 120 |

The shared `capped` helper takes the ceiling and records what it cut; only
`api_session` and `api_analysis` surface it, under `truncated`. Every ceiling
reaches the database as a `LIMIT`, `HOOK_MAX_FIRES` included: `api_hook` matches
the hook name in SQL through `HOOK_KEY` (a `hook_name`/`hook_event` column since
\#179) — cutting first would return the last fires of the busiest hook, not the
one asked for.

`prompt_stats` cuts on an aggregate: a turn is a group of events, so the cap
orders on `MIN(ts)` and its first query is the population. The two queries
enriching it (the `api_request` sums, the `user_prompt` text) skip a `prompt_id`
that query did not select and add none of their own.

## Static files

`PAGE` and `ASSETS` are read once at startup from `WEB_DIR` — the `web/` folder
inside the package, resolved from `__file__`, so the served frontend ships with
the package. A missing file fails the boot rather than serving a blank dashboard;
editing an asset requires a restart.

`ASSETS` is an allowlist keyed by request path. No filesystem path is ever built
from client input, so `/assets/../ccdash.py` is simply a missing key.

`ccdash.css` is served like the rest but is a **build output** — edit
`styles/input.css` at the repo root. `styles/` is in neither the allowlist nor
the Docker image: the compiled stylesheet is committed, so the server never
builds anything.

## The host allowlist

`do_GET` answers only a request whose `Host` names a host we serve under, and
`403`s the rest before the page, assets and payloads alike. The refusal is
journalled under `HOST_REFUSED`, carrying neither the name nor the path (both are
the caller's to choose, and Diagnostics serves that journal). One row per
process, not per refusal — the caller is unauthenticated and free to repeat, and
each row takes the write lock ingestion needs. The detail goes to stderr under
`-v`.

It closes DNS rebinding, which the `Origin` refusal does not: that one guards the
write path, and a page resolving its own domain to this port is after the read
path. `Host` is the header a browser fills from its address bar and no page can
forge, and an IP literal cannot be rebound at all.

`allowed_hosts` is built at start-up by `host_allowlist()` from `LOOPBACK_HOSTS`,
the `--host` value, every `--allow-host`, and the comma-separated
`CCDASH_ALLOW_HOST` (how `compose.yml` declares one, since it cannot append a
flag conditionally). A wildcard bind contributes nothing: `0.0.0.0` names every
interface and no host, and the server says so on start-up rather than leaving an
operator with an unexplained `403`.

`host_name()` normalises both sides to a bare lowercase name: the port is dropped
(it says nothing about who is calling) and IPv6 loses its brackets. A request
with no `Host` reaches the check as the empty name, which nothing allows.

**GET only.** An exporter is not a browser and cannot be made to read anything
back, so `do_POST` keeps answering whatever base URL it was configured with.

## Invariants

- A GET is answered under a name in `allowed_hosts` and nowhere else.
- Python standard library only. No `requirements.txt`, no outbound network, and
  no build step of its own.
- `TABLES` is the whole schema; a fresh database is created complete from it.
  There is no boot-time migration — an existing base is caught up by a one-off
  hand `ADD COLUMN` + backfill, and nothing drops or rewrites an existing column.
- OTLP attributes naming the user or the organization are dropped on ingestion
  — the six keys of `DROP_ATTRS`. Nothing else is: prompt text, shell commands,
  absolute file paths and the raw attribute blob are stored as sent.
- The allowlist is not negotiable: never `os.path.join` a request path.
- **No authentication of any kind.** The default bind is `127.0.0.1`; see the
  warning in the README before changing it.
