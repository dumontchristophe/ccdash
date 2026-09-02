"""The routing table the handler dispatches on: one entry per /api/ endpoint,
each pointing at the module that owns it. Holds no query of its own.
"""

from collections.abc import Callable
from typing import Any

from .core import request
from .pages import analysis, costs, details, health, overview, sessions

# The canonical list of GET endpoints. Uniform `(params, filters)` signature —
# the filters are already decoded, each lambda reading only what its `api_*`
# needs.
API_ROUTES: dict[str, Callable[[dict[str, list[str]], request.Filters], Any]] = {
    "/api/overview": lambda params, filters: overview.api_overview(filters),
    "/api/projects": lambda params, filters: costs.api_projects(filters),
    "/api/sessions": lambda params, filters: sessions.api_sessions(filters),
    "/api/session": lambda params, filters: sessions.api_session(
        request.one_param(params, "id")
    ),
    "/api/analysis": lambda params, filters: analysis.api_analysis(filters),
    "/api/event": lambda params, filters: details.api_event(
        request.int_param(params, "id")
    ),
    "/api/subagent": lambda params, filters: details.api_subagent(
        request.int_param(params, "id")
    ),
    "/api/costs": lambda params, filters: costs.api_costs(filters),
    "/api/context": lambda params, filters: sessions.api_context(filters),
    "/api/calls": lambda params, filters: analysis.api_calls(
        request.one_param(params, "label"),
        filters,
        request.one_param(params, "session") or None,
        request.one_param(params, "prompt") or None,
        request.one_param(params, "file") or None,
    ),
    "/api/filters": lambda params, filters: overview.api_filters(),
    "/api/hook": lambda params, filters: health.api_hook(
        request.one_param(params, "name")
    ),
    "/api/prompt": lambda params, filters: details.api_prompt(
        request.one_param(params, "id")
    ),
    "/api/health": lambda params, filters: health.api_health(filters),
    # Liveness only. Whatever polls this -- the container healthcheck, among
    # others -- has no use for the database path or the prompt counters that
    # /api/health carries.
    "/health": lambda params, filters: {"ok": True},
}
