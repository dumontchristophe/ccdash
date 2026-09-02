# recap

"Here's what you did" over a window — a record of the work, **not** an analysis.
No signals, no severity, no improvement; it leans on no motif. Scope from
`SKILL.md` (`days` default 7, optional `project`).

The substance:

- **Topics** — from `/api/sessions` `title` and the `/api/analysis` prompts list.
- **Projects** — from `/api/projects`.
- **Files changed** — from each session's `files`.

Then a handful of stats worth surfacing — not a fixed list: the interesting ones
vary by project, so pick a few pertinent figures from `/api/overview` and
`/api/projects` (session count, active time, cost, commits/LOC, …) that fit what
this window actually covers.

A null `title`/`prompt_text` (`OTEL_LOG_USER_PROMPTS` off) drops the recap to
sessions, projects, files and stats — say so, name no topics.
