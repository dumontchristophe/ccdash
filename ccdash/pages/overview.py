"""The landing page: the dropdown values, the headline figures and the /api/overview
payload built around them.
"""

from datetime import datetime
from typing import Any

from ..core import aggregates, request, store
from . import analysis, costs


def _dropdown_values(column: str) -> list[str]:
    """The distinct values one dropdown offers, from both tables."""
    return [
        row["value"]
        for row in store.query(
            "SELECT DISTINCT %(column)s value FROM metric_points "
            "WHERE %(column)s IS NOT NULL "
            "UNION SELECT DISTINCT %(column)s FROM events "
            "WHERE %(column)s IS NOT NULL ORDER BY value" % {"column": column}
        )
        if row["value"]
    ]


def api_filters() -> dict[str, list[str]]:
    """What the two dropdowns offer, from both tables: metrics and logs are
    separate exports, and every analysis view reads events."""
    hosts = _dropdown_values("host")
    projects = _dropdown_values("project")
    # Without OTEL_RESOURCE_ATTRIBUTES=project=..., sessions arrive with none:
    # offer the label api_projects groups them under.
    if store.query_row(
        "SELECT 1 FROM metric_points WHERE project IS NULL LIMIT 1"
    ) or store.query_row("SELECT 1 FROM events WHERE project IS NULL LIMIT 1"):
        projects.append("(undefined)")
    return {"hosts": hosts, "projects": projects}


def headline_figures(
    scope: request.Scope,
    tokens: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The four figures the Overview cards lead with, over whatever window is
    given -- it runs twice, on the filter window and on the one before it.
    `tokens` lets a caller hand over a breakdown it already has."""
    return {
        "sessions": store.query_value(
            *aggregates.scoped("COUNT(*)", "(" + aggregates.SPENT_SESSIONS + ")", scope)
        ),
        "tool_calls": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE name='tool_result'" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "cost": store.query_value(
            *aggregates.scoped(
                "SUM(value)",
                "metric_points WHERE name='claude_code.cost.usage'"
                + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "tokens": sum(
            (aggregates.tokens_by_type(scope) if tokens is None else tokens).values()
        ),
    }


def api_overview(filters: request.Filters) -> dict[str, Any]:
    """The full /api/overview payload: the headline KPIs with their change over
    the preceding window, the token breakdown, per-family call counts, the weekly
    activity rhythm and the per-project, skill, MCP and delegation summaries.

    `prev` carries the same figures over the window before this one, so each card
    shows its change; it is None on the all-time window (days=0) or when the
    preceding window is empty, which would otherwise read as -100%.
    """
    scope = filters.scope()
    tokens = aggregates.tokens_by_type(scope)

    def metric_total(name: str, extra: str = "") -> Any:
        return store.query_value(
            *aggregates.scoped(
                "SUM(value)",
                "metric_points WHERE name=?" + aggregates.SCOPE_MARK + extra,
                scope,
                args=(name,),
            )
        )

    head = headline_figures(scope, tokens)
    prompts = store.query_value(
        *aggregates.scoped(
            "COUNT(*)",
            "events WHERE name='user_prompt'" + aggregates.SCOPE_MARK,
            scope,
        )
    )
    skills, mcp = analysis.inventory_stats(scope)
    by_agent = analysis.delegation_types(scope)

    # The preceding window, so each card carries its change. days=0 already
    # covers everything, and an empty window would read as -100%.
    prev = None
    if filters.days:
        earlier = headline_figures(filters.scope(previous=True))
        prev = earlier if any(earlier.values()) else None

    # How often each family answered, not what it cost: Haiku answers often
    # for nearly nothing, which no cost breakdown shows.
    model_calls: dict[str | None, Any] = {}
    for row in store.query(
        *aggregates.scoped(
            "model, COUNT(*) calls",
            "events WHERE name='api_request' AND model IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            group="model",
        )
    ):
        family = aggregates.short_model(row["model"])
        model_calls[family] = model_calls.get(family, 0) + row["calls"]

    # Weekly rhythm, a 7x24 grid of telemetry points, Monday first. Bucketed so
    # the zone conversion runs once per bucket; a quarter of an hour and not an
    # hour, since a 30- or 45-minute offset would straddle two local hours.
    rhythm = [[0] * 24 for _ in range(7)]
    for row in store.query(
        *aggregates.scoped(
            "ts/900 bucket, COUNT(*) points",
            "metric_points WHERE 1=1" + aggregates.SCOPE_MARK,
            scope,
            group="bucket",
        )
    ):
        local = datetime.fromtimestamp(row["bucket"] * 900)
        rhythm[local.weekday()][local.hour] += row["points"]

    return {
        "kpi": dict(
            head,
            prompts=prompts,
            active_seconds=metric_total("claude_code.active_time.total"),
            commits=metric_total("claude_code.commit.count"),
            loc_add=metric_total(
                "claude_code.lines_of_code.count", " AND attr_type='added'"
            ),
            loc_del=metric_total(
                "claude_code.lines_of_code.count", " AND attr_type='removed'"
            ),
        ),
        "prev": prev,
        # No weighted breakdown and no daily series: spending is a Costs figure.
        "tokens": tokens,
        "model_calls": model_calls,
        "rhythm": rhythm,
        "projects": costs.api_projects(filters),
        "skills": skills,
        "mcp": mcp,
        "delegation_types": by_agent,
    }
