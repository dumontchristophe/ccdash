# Changelog

## Unreleased

### Fixed

- `compose.yml` now passes `CCDASH_TZ` and `CCDASH_ENABLE_UPDATE_CHECK` from `.env` into the container; setting them in `.env` had no effect on a compose deployment.

## 1.1.0 — 2026-09-08

### Added

- A setup modal with copy-paste configuration to point Claude Code's telemetry at ccdash.
- The running version in the sidebar footer, reported by a new `/api/version` endpoint.
- A once-a-day check for a newer release, flagged in the sidebar footer by a status dot that links to the release; opt-out via `CCDASH_ENABLE_UPDATE_CHECK`.
- `CCDASH_TZ`: one configurable IANA timezone every date is bucketed and displayed in, across the cost day chart, the rhythm grid and every timestamp. Unset means UTC; an unknown name warns and falls back to UTC.
- `start_date` / `end_date` query parameters on every `/api/*` route: an explicit, inclusive `YYYY-MM-DD` range in `CCDASH_TZ`, either bound optional. A range wins over `days`.

### Changed

- No server-side `days` default: a bare API call reads the whole history (the frontend always sends `days`). A non-numeric `days` is now a 400 rather than seven days.
- Timestamps render in the server's `CCDASH_TZ` zone (UTC by default) rather than the browser's, so day boundaries agree with the backend.
- Sessions are named by their generated title.
- Release tags are now bare `major.minor.patch`, checked in CI against `__version__` (the single source of truth).
- Telemetry logging defaults are on, with the privacy posture and configuration clarified across Claude surfaces.

## 1.0.0 — 2026-09-01

### Added

- An Overview: headline figures each compared against the previous period, model, skill and sub-agent breakdowns, a project grid, and a rhythm grid of activity by day and hour.
- A session view: the context-pressure curve, cumulative and weighted tokens, cost, request origins, and a named timeline of permission-mode switches, MCP connections, API and internal errors, at-mentions and hook timings — hooks over 500 ms stand out.
- Per-session tabs for Files, Bash, Sub-agents, Prompts and Errors & Permissions, each saying when a list was cut.
- A prompt view: everything a single turn set off, reachable from any call.
- Per-call tables with a detail modal for Bash, failures, sub-agents and hooks, and an event inspector in a modal.
- A Costs page: the cost curve, the weighted token breakdown, and cost by real request origin.
- A Diagnostics page: the ingestion journal, hook latency and failures, and the temporality and idle checks.
- A light/dark theme.
- Auto-refresh and a manual refresh on every view, for watching a live session.
- Responsiveness
- OTLP metrics and logs over HTTP/JSON, gzip, deflate and chunked bodies included.
