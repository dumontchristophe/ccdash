"""Diagnostics: hook overhead, the ingestion journal and the counters the Health
view reads. The day window bounds every scan of `events` and `metric_points`, so
the page no longer grows with total history; host and project stay global -- see
`request.Filters.scope(window_only=True)`. The ingestion journal, the database
path and the OTEL `temporality` flag describe the whole store, not the window,
and take no scope.
"""

import os
from typing import Any

from ..core import aggregates, request, store


def hook_stats(scope: request.Scope) -> list[dict[str, Any]]:
    """Hook overhead by hook name, over the window. The fields are columns
    summed by SQLite, so the cost is the ix_ev_name seek, not a JSON decode per
    event. `events.name` is spelled out in the WHERE: the grouped name is
    aliased `name` too."""
    return store.query_dicts(
        *aggregates.scoped(
            aggregates.HOOK_KEY + " name, MAX(hook_event) event, COUNT(*) fires, "
            "SUM(" + aggregates.HOOK_MS + ") total_duration_ms, "
            "MAX(" + aggregates.HOOK_MS + ") max, "
            "SUM(" + aggregates.HOOK_ERR + ") err, "
            "SUM(" + aggregates.HOOK_BLOCK + ") block, "
            "AVG(" + aggregates.HOOK_MS + ") avg",
            "events WHERE events.name='hook_execution_complete'"
            + aggregates.SCOPE_MARK,
            scope,
            # Positional, so grouping and ranking read the aliases, not the
            # `name` column they shadow.
            group="1",
            order="fires DESC, 1",
        )
    )


HOOK_MAX_FIRES = 200


def api_hook(name: str) -> dict[str, Any]:
    """One hook's recent fires (hook_execution_complete) plus what it was
    registered with (hook_registered). Global scope.

    The name is matched in SQL, so HOOK_MAX_FIRES cuts this hook's own fires; cut
    first, a rare hook would come back empty."""
    rows = store.query(
        "SELECT ts, session_id, project, "
        "hook_event event, num_hooks hooks, "
        + aggregates.HOOK_MS
        + " duration_ms, "
        + aggregates.HOOK_ERR
        + " err, "
        + aggregates.HOOK_BLOCK
        + " block "
        "FROM events WHERE name='hook_execution_complete' AND "
        + aggregates.HOOK_KEY
        + "=?"
        " ORDER BY ts DESC LIMIT %d" % HOOK_MAX_FIRES,
        (name,),
    )
    event = next((row["event"] for row in rows if row["event"]), None)
    hooks = max((store.as_int(row["hooks"], 0) for row in rows), default=0)
    fires = [
        {
            "ts": row["ts"],
            "session_id": row["session_id"],
            "project": row["project"],
            "duration_ms": row["duration_ms"],
            "err": row["err"],
            "block": row["block"],
        }
        for row in rows
    ]
    # A fire is named "<event>:<matcher>" when the matcher is exported, so the
    # registrations are narrowed on it too: PreToolUse:Bash would otherwise list
    # every PreToolUse entry. `or None`, so a trailing separator narrows nothing.
    matcher = name.split(":", 1)[1] or None if ":" in name else None
    # The same entry is re-registered on every startup, so the grouping is the
    # dedup — and MIN(id) keeps the order the first occurrences were stored in.
    # IS and not =, so a hook with no event at all still matches its own NULL.
    registrations = store.query_dicts(
        "SELECT hook_source source, hook_type type, hook_matcher matcher "
        "FROM events WHERE name='hook_registered' "
        "AND hook_event IS ? "
        "AND (? IS NULL OR hook_matcher IS NULL OR hook_matcher = ?) "
        "GROUP BY 1, 2, 3 ORDER BY MIN(id)",
        (event, matcher, matcher),
    )
    return {
        "name": name,
        "event": event,
        "hooks": hooks,
        "regs": registrations,
        "fires": fires,
    }


def api_health(filters: request.Filters) -> dict[str, Any]:
    """The full /api/health payload: hook overhead, the ingestion journal, the
    OTEL temporality flag, the idle sessions and the per-name event and metric
    counts the Diagnostics view reads.

    Only the day window applies: the counters bound their scans by the reader's
    window, but host and project stay global on purpose. The ingestion journal,
    the temporality flag and the database path describe the whole store, so they
    take no scope.
    """
    # Time only: the counters below bound their scans by the reader's window,
    # but Diagnostics stays global across hosts and projects on purpose.
    scope = filters.scope(window_only=True)
    return {
        "hooks": hook_stats(scope),
        # The ingestion journal and the database path describe the whole store,
        # not the window, so they take no scope.
        "ingest": store.query_dicts(
            "SELECT kind, SUM(accepted) accepted, SUM(skipped) skipped, "
            "MAX(ts) last, COUNT(*) batches "
            "FROM ingest_log GROUP BY kind"
        ),
        "notes": store.query_dicts(
            "SELECT note, COUNT(*) batches, MAX(ts) last FROM ingest_log "
            "WHERE note IS NOT NULL GROUP BY note "
            "ORDER BY batches DESC LIMIT 10"
        ),
        # Read first, the docs say: the OTEL temporality flag, not a windowed
        # metric. Left global so a correctly configured export never reads as
        # missing merely because the active window held no point.
        "temporality": [
            row["temporality"]
            for row in store.query(
                "SELECT DISTINCT temporality FROM metric_points "
                "WHERE temporality IS NOT NULL"
            )
        ],
        # The sessions every other page leaves out, listed so the exclusion is
        # inspectable. No LIMIT: the table sorts client-side, so a top-N would
        # be ranked on whichever column the server picked.
        "idle": store.query_dicts(
            *aggregates.scoped(
                "session_id, project, MIN(ts) started_at, COUNT(*) points",
                "metric_points WHERE session_id IS NOT NULL" + aggregates.SCOPE_MARK,
                scope,
                group="session_id HAVING SUM(CASE WHEN name IN "
                + aggregates.SPEND_METRICS
                + " THEN value ELSE 0 END)=0",
                order="started_at DESC",
            )
        ),
        "unknown": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE name='unknown'" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        # `WHERE 1` opens the clause the window appends to, since these counts
        # have no predicate of their own.
        "event_names": store.query_dicts(
            *aggregates.scoped(
                "name, COUNT(*) points",
                "events WHERE 1" + aggregates.SCOPE_MARK,
                scope,
                group="name",
                order="points DESC",
            )
        ),
        "metric_names": store.query_dicts(
            *aggregates.scoped(
                "name, COUNT(*) points",
                "metric_points WHERE 1" + aggregates.SCOPE_MARK,
                scope,
                group="name",
                order="points DESC",
            )
        ),
        "metric_points": store.query_value(
            *aggregates.scoped(
                "COUNT(*)", "metric_points WHERE 1" + aggregates.SCOPE_MARK, scope
            )
        ),
        "masked_mcp": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE tool_name='mcp_tool' AND mcp_server IS NULL"
                + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "prompts_total": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE name='user_prompt'" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "prompts_text": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE name='user_prompt' AND prompt_text IS NOT NULL"
                + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "slash_seen": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE prompt_text LIKE '/%'" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "renames": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE prompt_text LIKE '/rename%'" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "delegation_calls": store.query_value(
            *aggregates.scoped(
                "COUNT(*)",
                "events WHERE (agent_type IS NOT NULL "
                "OR tool_name IN ('Task','Agent'))" + aggregates.SCOPE_MARK,
                scope,
            )
        ),
        "db": store.db_path,
        "db_size": os.path.getsize(store.db_path)
        if store.db_path and os.path.exists(store.db_path)
        else 0,
    }
