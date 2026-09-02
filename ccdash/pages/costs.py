"""What was spent: the per-project rows and the /api/costs breakdowns."""

from typing import Any

from ..core import aggregates, request, store


def api_projects(filters: request.Filters) -> list[dict[str, Any]]:
    """One row per project over the window: the spend, token and session totals,
    enriched with the model families each answered and its tool-call count.

    Rows are grouped in SQL and the enrichment adds no per-project query: the
    models and tool counts are grouped once and joined in Python. Sessions with
    no project land under `(undefined)`, and a missing metric reads as 0, not the
    NULL SQLite would sort as a string.
    """
    scope = filters.scope()
    out = store.query_dicts(
        *aggregates.scoped(
            "COALESCE(project,'(undefined)') project, "
            "COUNT(DISTINCT session_id) sessions, "
            "SUM(CASE WHEN name='claude_code.cost.usage' THEN value END) cost, "
            "SUM(CASE WHEN name='claude_code.token.usage' THEN value END) tokens, "
            # 'cacheCreation' is the OTLP spelling as stored, the alias the
            # canonical name (aggregates.SESSION_TOTALS does the same).
            "SUM(CASE WHEN name='claude_code.token.usage' AND attr_type='cacheCreation' "
            "         THEN value END) cache_creation_tokens, MAX(ts) last",
            # Two markers -- the spent-session subquery and the outer window --
            # so `scoped` passes the scope args twice, aligned.
            "metric_points WHERE session_id IN ("
            + aggregates.SPENT_SESSIONS
            + ")"
            + aggregates.SCOPE_MARK,
            scope,
            group="1",
            order="cost DESC",
        )
    )
    # Grouped once rather than asked per project: the enrichment runs no query.
    models: dict[str, set[str | None]] = {}
    for row in store.query(
        *aggregates.scoped(
            "DISTINCT COALESCE(project,'(undefined)') project, model",
            "metric_points "
            "WHERE model IS NOT NULL AND query_source='main'" + aggregates.SCOPE_MARK,
            scope,
        )
    ):
        # NOT NULL does not exclude the empty string, and short_model('') is
        # None, which sorts against nothing.
        if row["model"]:
            models.setdefault(row["project"], set()).add(
                aggregates.short_model(row["model"])
            )
    tools = {
        row["project"]: row["calls"]
        for row in store.query(
            *aggregates.scoped(
                "COALESCE(project,'(undefined)') project, COUNT(*) calls",
                "events WHERE name='tool_result'" + aggregates.SCOPE_MARK,
                scope,
                group="1",
            )
        )
    }
    for project in out:
        project["models"] = sorted(models.get(project["project"], ()))
        project["tools"] = tools.get(project["project"]) or 0
        for key in ("cost", "tokens", "cache_creation_tokens"):
            project[key] = project[key] or 0
    return out


def api_costs(filters: request.Filters) -> dict[str, Any]:
    """The full /api/costs payload: the daily cost series stacked by model family,
    the per-family token and cost totals, the weighted token view and the spend
    broken down by request origin.

    `series` carries one entry per day with a key per family, `families` the
    family names those keys draw from, and `origins` splits cost and output
    tokens by the real request origin `metric_points` cannot see -- flagged
    `is_main_thread` so the page can draw the share off the main thread.
    """
    scope = filters.scope()
    stack: dict[str, dict[str, Any]] = {}
    for row in store.query(
        *aggregates.scoped(
            "date(ts,'unixepoch','localtime') d, model, SUM(value) value",
            "metric_points WHERE name='claude_code.cost.usage'" + aggregates.SCOPE_MARK,
            scope,
            group="d, model",
            order="d",
        )
    ):
        family = aggregates.short_model(row["model"]) or "?"
        day = stack.setdefault(row["d"], {})
        day[family] = day.get(family, 0) + (row["value"] or 0)
    series = [{"d": day, **costs} for day, costs in sorted(stack.items())]
    families = sorted({family for costs in stack.values() for family in costs})

    per_model: dict[str | None, dict[str, Any]] = {}
    for row in store.query(
        *aggregates.scoped(
            "model, attr_type, SUM(value) value",
            "metric_points "
            "WHERE name='claude_code.token.usage' AND model IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            group="model, attr_type",
        )
    ):
        model_family = aggregates.short_model(row["model"])
        family_totals = per_model.setdefault(model_family, {})
        token_type = aggregates.TOKEN_TYPES.get(row["attr_type"], row["attr_type"])
        family_totals[token_type] = family_totals.get(token_type, 0) + (
            row["value"] or 0
        )
    # No model filter here: the Overview's Est. cost sums every cost point, so
    # dropping the model-less ones would leave Total cost short. `?` collects
    # them, matching the daily series above.
    for row in store.query(
        *aggregates.scoped(
            "model, SUM(value) value",
            "metric_points WHERE name='claude_code.cost.usage'" + aggregates.SCOPE_MARK,
            scope,
            group="model",
        )
    ):
        model_family = aggregates.short_model(row["model"]) or "?"
        family_totals = per_model.setdefault(model_family, {})
        family_totals["cost"] = family_totals.get("cost", 0) + (row["value"] or 0)

    tokens = aggregates.tokens_by_type(scope)
    # Cost/tokens by real request origin: metric_points only carries a coarser
    # main/auxiliary/subagent version. Surfaces the spend on autocomplete
    # (prompt_suggestion) and on work no prompt asked for.
    origins = store.query_dicts(
        *aggregates.scoped(
            "COALESCE(query_origin,'?') src, SUM(COALESCE(cost_usd,0)) cost, "
            "SUM(COALESCE(out_tokens,0)) output_tokens, COUNT(*) calls",
            "events WHERE name='api_request'" + aggregates.SCOPE_MARK,
            scope,
            group="src",
        )
    )
    for origin in origins:
        # The page draws the share outside the main thread from this flag: the
        # list of names the main thread answers to stays on this side.
        origin["is_main_thread"] = origin["src"] in aggregates.MAIN_THREAD_ORIGINS
    return {
        "series": series,
        "families": families,
        "per_model": per_model,
        "tokens": tokens,
        "weights": aggregates.WEIGHTS,
        "weighted": aggregates.weighted_tokens(tokens),
        "by_project": api_projects(filters),
        "origins": sorted(origins, key=lambda x: -x["cost"]),
    }
