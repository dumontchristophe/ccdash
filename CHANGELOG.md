# Changelog

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
