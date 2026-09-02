# Contributing to ccdash

Running the dashboard is in the [README](README.md); what the database holds is
in [SECURITY.md](SECURITY.md); the conventions and non-negotiables are in
[CLAUDE.md](CLAUDE.md).

## Reporting a bug or requesting a feature

Open an issue through one of the templates — the blank box is disabled, so pick
**Bug report** or **Feature request** and fill the fields. A **security**
problem never goes in a public issue: report it privately through the advisory
link on the chooser (see [SECURITY.md](SECURITY.md)).

## Rules

- **No runtime dependency.** Python stdlib and ES modules only — no pip, npm,
  manifest or lockfile. `ruff`, `mypy` and the `tailwindcss` binary are dev
  tools, pinned in CI, never in the image.
- **Nothing outbound.** No CDN, no font fetch, no update check, no telemetry of
  our own.

## Checks

Python 3.12 (pinned in `ci.yml`, `ruff.toml`, `Dockerfile`). CI runs exactly
these:

```bash
python3 -m unittest discover -s tests
ruff check ccdash tests && ruff format --check ccdash tests
mypy --python-version 3.12 --ignore-missing-imports ccdash
```

## Rebuilding the stylesheet

Only if you touch `class` attributes. Rebuild with the standalone
[Tailwind CSS](https://github.com/tailwindlabs/tailwindcss/releases) binary
(≥ 4.1.0) and commit the result — `tests/test_build.py` catches a stale one:

```bash
tailwindcss -i styles/input.css -o ccdash/web/assets/ccdash.css
```
