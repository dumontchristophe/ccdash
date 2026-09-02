---
name: ccdash
description: Read the ccdash telemetry API to understand Claude Code sessions and turn what you find into environment improvements — `analyse` a run or a window for signals, `recap` what was worked on, or `ask` a one-off question.
disable-model-invocation: true
---

# ccdash

Understand a Claude Code run from its telemetry, then propose environment
improvements for the next one. The API is the sole source of every figure —
where it has no answer, say so. Never a guess, never a figure from memory.

## Reach the server

Read `env.OTEL_EXPORTER_OTLP_ENDPOINT` from `~/.claude/settings.json` — ingest
and API share one port, so that value is the API root. Ask the user when the key
is absent. Query with `curl -s -m 8 "<base>/api/…"`.

Read `refs/api.md` before the first request: it holds the routes, the filters,
and the semantics the raw JSON hides.

## Validate before reporting

GET `/api/health?days=0` first and read it silently — surface a line only when a
check fails. `temporality` must be `[1]` (delta); a `2` is cumulative, so every
token and cost sum is wrong — say so and withhold them (`[]` means no metric
carried the field). And absent is not zero: a null `title`/`prompt_text` means
`OTEL_LOG_USER_PROMPTS` was off, not an empty prompt (`prompts_text` below
`prompts_total` dates the gap) — name the missing source rather than reporting a
zero.

## Resolve the scope

Shared by `analyse` and `recap`; both read this rather than restating it.

- **No scope named → the live session.** Its id is the `CLAUDE_CODE_SESSION_ID`
  environment variable, the ccdash `session_id` exactly.
- **A period and/or project named → the window.** `days`, `host`, `project`
  filter it (values from `/api/filters`); absent `days` means 7. When the window
  spans more than one host or project and the user named neither, take the
  distinct `host` and `project` of `/api/sessions?days=N` and ask which — one
  question listing the values with their session counts, plus `all`. A single
  value is used without asking. To default to "this machine", read the host from
  `OTEL_RESOURCE_ATTRIBUTES` in settings.json.

## Route on the keyword

The invocation keyword picks the mode. Default to `analyse` when none is named.

| Keyword | Mode | Read |
|---|---|---|
| `analyse` | signals and improvement candidates | `modes/analyse.md` |
| `recap` | the record of work over a window | `modes/recap.md` |
| `ask` | a one-off question | `modes/ask.md` |
