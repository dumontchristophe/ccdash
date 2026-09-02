# analyse

Signals from the telemetry, then the changes they argue for. The scope signal
(from `SKILL.md`) picks the read:

- **The live session** — the run in progress. GET
  `/api/session?id=$CLAUDE_CODE_SESSION_ID`. Telemetry exports in delta batches,
  so the last few turns may not be ingested yet — read the tail as provisional.
  Keep that internal; surface it only when it changes a figure you report. One
  story, this run.
- **The window** — many runs, hunting recurring motifs rather than one story. A
  motif repeats across runs: the same `error_type`, the same slow tool, the same
  compaction, one project hotter than the rest.

Report both sections.

## Findings

Concise prose, not a dump. The two or three things that matter about this run or
these runs, each led by the finding with its figure cited after. Tie every
figure to its scope — a session-wide total or a single outlier — so the two are
never read as one.

## Improvement candidates

Read off the telemetry. Each candidate names the **signal** in the data and the
change it argues for — to the environment, or to how the user works. Order by
severity.

**Open the outliers.** Aggregates hide the story — scan the axes (cost, duration,
calls, errors, retries), find where a session or turn breaks from the rest, and
read its `prompt_text` in sequence. Cite the mechanism, not the number — *why* it
ran long, burned tools, lost the thread, or cost.

Draw from:

- **Context pressure** — `/api/context`: auto-compactions, a `max_context` near
  the model window, a high tool-calls-per-prompt `ratio`. Argues for splitting
  the work, dispatching a sub-agent, or trimming a bloated `CLAUDE.md`/`AGENTS.md`.
- **Tool economy** — slow or large tool results, a Bash command run many ways, a
  sub-agent that returned little for its tokens. Argues for a tighter command, a
  script, or a cheaper tool.
- **Navigation** — many `Read`/`Grep` before the first `Edit` on a file. Argues
  for a navigation pointer so the next run finds it first.
- **Repeated failure** — the same `error_type` across turns or runs. Argues for
  an automated check (lint, type, test, filesystem linter) that would have caught
  it, or a reviewer rule.
- **Cost & model** — a large non-`main_thread` share in `/api/costs.origins`, a
  heavy model on light work. Argues for overhead to cut or a model to downgrade.
- **Prompting & skills** — the user's `prompt_text` and the `skills` sections of
  `/api/session` and `/api/overview`: vague or oversized prompts, a turn
  re-asked several ways, a skill that exists but never fires, a slash command
  mistyped (`slash_seen` in `/api/health`). Argues for a clearer prompt habit, a
  skill to reach for, or a skill's trigger wording to sharpen.
- **Steering no-ops** — behaviour the figures show the agent already gets right,
  still spelled out in a steering file. Argues to delete the line.
- **Telemetry config** — a `2` in `temporality`, `prompts_text` below
  `prompts_total`, a rising `unknown`, `masked_mcp` above zero. Argues to fix the
  exporter so the next window's data is complete.
