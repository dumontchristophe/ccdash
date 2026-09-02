# Domain docs

How the engineering skills should consume this repo's domain documentation when
exploring the codebase. Layout: **single-context**.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`**: the ADRs touching the area you're about to work in.

`CONTEXT.md` does not exist yet; `docs/adr/` holds ADRs 0001–0003. If a file
listed here is absent, **proceed silently** —
don't flag it, don't propose creating it upfront. `/domain-modeling` creates them
lazily, when a term or a decision actually gets resolved.

What does exist is the prose record under `docs/`, and it is not optional:
`CLAUDE.md` names one page per area (backend, frontend, dashboard, reference).
**Read the page covering the area before editing it.** A `CONTEXT.md` would sit
alongside those pages as a glossary, not replace them.

## File structure

```
/
├── CONTEXT.md          ← glossary (does not exist yet)
├── docs/
│   ├── adr/            ← decisions (0001–0003)
│   ├── agents/         ← this file, the tracker and the labels
│   ├── backend.md      ← the existing prose record
│   ├── frontend.md
│   ├── dashboard.md
│   └── reference.md
└── app/
```

There is no `CONTEXT-MAP.md` and no per-context `src/<context>/`: this is a
single Python package plus a static ES-module frontend, not a monorepo.

## Use the glossary's vocabulary

When your output names a domain concept — an issue title, a refactor proposal, a
hypothesis, a test name — use the term as `CONTEXT.md` defines it, and failing
that, the term `docs/reference.md` uses. Cost semantics and the OTEL variables
are defined there and nowhere else; don't drift to synonyms.

If the concept isn't in the glossary yet, that's a signal: either you're
inventing language the project doesn't use (reconsider), or there's a real gap
(note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it rather than silently
overriding:

> _Contradicts ADR-0007 (…), but worth reopening because…_

The same holds for the **Non-negotiables** in `CLAUDE.md`, which act as ADRs the
repo never wrote down: no runtime dependency, no edit to the generated CSS,
`escapeHtml` on every payload value, no `os.path.join` on a request path,
`DROP_ATTRS` as the whole of the stripping, no authentication. Contradicting one
is a conversation with the user, never a silent decision.
