"""The analyses a window or a session is read through: tools, files, bash calls,
errors, delegations and turns, plus the /api/analysis and /api/calls endpoints
built on them.

Every function here takes a Scope, so the same aggregate serves the global page
and a single session.
"""

import json
from typing import Any

from ..core import aggregates, request, store

# The five /api/analysis sections that ship a row per call or per turn, and where
# each stops. An aggregate covers the whole window, so a cut list reads as a
# complete one unless `truncated` says otherwise.
ANALYSIS_CAPS = {
    "bash": 300,
    "errors": 200,
    "api_errors": 200,
    "subagents": 300,
    "prompts": 300,
}


def tool_stats(scope: request.Scope) -> list[dict[str, Any]]:
    """Tool stats over any scope: global or a single session."""
    base = store.query(
        *aggregates.scoped(
            "label, tool_name, COUNT(*) calls, "
            "SUM(success='false') failures, "
            "SUM(COALESCE(result_bytes,0)) total_bytes, "
            "AVG(duration_ms) avg_duration_ms",
            "events WHERE name='tool_result'" + aggregates.SCOPE_MARK,
            scope,
            group="label",
        )
    )
    # Median and p95 by SQLite, never read row by row into Python: ROW_NUMBER
    # ranks each label's sizes and the nearest-rank pick is round(f*(n-1)).
    # SQLite rounds half away from zero, so an exact .5 lands one rank above
    # Python's round-half-to-even -- immaterial on a byte-size percentile.
    percentiles = {
        row["label"]: (row["median_bytes"], row["p95"])
        for row in store.query(
            *aggregates.windowed(
                "WITH sized AS (SELECT label, result_bytes, "
                "ROW_NUMBER() OVER (PARTITION BY label ORDER BY result_bytes)-1 rn, "
                "COUNT(*) OVER (PARTITION BY label) n "
                "FROM events WHERE name='tool_result' AND result_bytes IS NOT NULL"
                + aggregates.SCOPE_MARK
                + ") SELECT label, "
                "MAX(CASE WHEN rn=CAST(ROUND(0.5*(n-1)) AS INT) THEN result_bytes END) "
                "median_bytes, "
                "MAX(CASE WHEN rn=CAST(ROUND(0.95*(n-1)) AS INT) THEN result_bytes END) "
                "p95 FROM sized GROUP BY label",
                scope,
            )
        )
    }
    grand = sum(row["total_bytes"] or 0 for row in base) or 1
    out = []
    for row in base:
        median_bytes, p95 = percentiles.get(row["label"], (0, 0))
        label = row["label"] or "?"
        kind = next(
            (
                tool_kind
                for prefix, tool_kind in aggregates.KINDS
                if label.startswith(prefix)
            ),
            "Native",
        )
        out.append(
            {
                "label": label,
                "kind": kind,
                "calls": row["calls"],
                "failures": row["failures"] or 0,
                "total_bytes": row["total_bytes"] or 0,
                "median_bytes": median_bytes or 0,
                "p95": p95 or 0,
                "avg_duration_ms": row["avg_duration_ms"] or 0,
                "share": 100.0 * (row["total_bytes"] or 0) / grand,
            }
        )
    return sorted(out, key=lambda x: -x["total_bytes"])


def file_stats(scope: request.Scope, limit: int = 500) -> list[dict[str, Any]]:
    """Files the scope changed, one row per path. `Read` is left out: the point
    is what was written, and a read outnumbers a change several times over."""
    return store.query_dicts(
        *aggregates.scoped(
            "file_path, COUNT(*) calls, "
            "SUM(success='false') failures, "
            "SUM(tool_name='Edit') edits, SUM(tool_name='Write') writes, "
            "MAX(ts) last",
            "events WHERE name='tool_result' AND file_path IS NOT NULL "
            "AND tool_name IN ('Edit','Write')" + aggregates.SCOPE_MARK,
            scope,
            group="file_path",
            order="calls DESC",
            limit=limit,
        )
    )


def bash_calls(
    scope: request.Scope,
    limit: int = ANALYSIS_CAPS["bash"],
) -> list[dict[str, Any]]:
    """Per-invocation Bash calls, newest first. The human description and full
    command come from the parsed `params` column; the row id opens api_event."""
    out = []
    for row in store.query(
        *aggregates.scoped(
            "id, ts, session_id, project, duration_ms, result_bytes, success, "
            "error_type, bash_cmd, params",
            "events WHERE name='tool_result' AND bash_cmd IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            order="ts DESC",
            limit=limit,
        )
    ):
        params = json.loads(row["params"]) if row["params"] else {}
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "session_id": row["session_id"],
                "project": row["project"],
                "desc": (
                    params.get("description") if isinstance(params, dict) else None
                )
                or row["bash_cmd"],
                "cmd": row["bash_cmd"],
                "duration_ms": row["duration_ms"],
                "bytes": row["result_bytes"],
                "success": aggregates.success_bool(row["success"]),
                "error_type": row["error_type"],
            }
        )
    return out


def inventory_stats(
    scope: request.Scope,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The skill and MCP-server inventories over the window, each a use-count per
    name, most used first. Returned as a `(skills, mcp)` pair. A name the export
    masked lands under `(masked)`.
    """
    return (
        store.query_dicts(
            *aggregates.scoped(
                "COALESCE(skill_name,'(masked)') name, COUNT(*) uses",
                "events WHERE name='skill_activated'" + aggregates.SCOPE_MARK,
                scope,
                group="1",
                order="uses DESC",
            )
        ),
        store.query_dicts(
            *aggregates.scoped(
                "COALESCE(mcp_server,'(masked)') name, COUNT(*) uses",
                "events WHERE name='tool_result' AND (mcp_server IS NOT NULL OR "
                "tool_name='mcp_tool')" + aggregates.SCOPE_MARK,
                scope,
                group="1",
                order="uses DESC",
            )
        ),
    )


def errors_calls(
    scope: request.Scope,
    limit: int = ANALYSIS_CAPS["errors"],
) -> list[dict[str, Any]]:
    """Per-invocation failing tool_result calls, newest first. The row id opens
    api_event for the full detail.

    `cmd` comes along because Claude Code sends one fixed string for every
    non-zero shell exit -- "Shell command failed", no exit code, no stderr. The
    command is the only field that tells two such failures apart.
    """
    out = []
    for row in store.query(
        *aggregates.scoped(
            "id, ts, session_id, project, label, tool_name, error_type, "
            "bash_cmd, error_text",
            "events WHERE name='tool_result' AND success='false'"
            + aggregates.SCOPE_MARK,
            scope,
            order="ts DESC",
            limit=limit,
        )
    ):
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "session_id": row["session_id"],
                "project": row["project"],
                "label": row["label"],
                "tool_name": row["tool_name"],
                "error_type": row["error_type"],
                "msg": row["error_text"],
                "cmd": row["bash_cmd"],
            }
        )
    return out


def provider_errors(
    scope: request.Scope,
    limit: int = ANALYSIS_CAPS["api_errors"],
) -> list[dict[str, Any]]:
    """One row per provider incident, newest first: what Anthropic's API refused
    or gave up on. Named for the provider, since the `api_` prefix in this file
    means an endpoint. The retry folding is in `docs/backend.md`."""
    sql = (
        """
-- MATERIALIZED is load-bearing: the correlated subquery in `link` would
-- otherwise re-scan the events table once per exhaustion.
WITH err AS MATERIALIZED (SELECT * FROM events WHERE name='api_error'{scope}),
    rex AS MATERIALIZED (SELECT * FROM events WHERE name='api_retries_exhausted'{scope}),
    link AS (SELECT o.id rex_id, (
        SELECT e.id FROM err e
         -- A second of slack, not equality: an incident emitted across an epoch
         -- boundary is stored as two timestamps. IS and not =, so two records
         -- carrying no prompt.id still pair -- NULL = NULL is NULL.
         WHERE e.session_id = o.session_id AND ABS(e.ts - o.ts) <= 1
           AND e.prompt_id IS o.prompt_id
         -- First is ingestion order: SQLite will not read the outer row from
         -- the ORDER BY of a correlated subquery.
         ORDER BY e.id LIMIT 1) err_id
      FROM rex o),
    pair AS (SELECT err_id, MIN(rex_id) rex_id FROM link
              WHERE err_id IS NOT NULL GROUP BY err_id)
SELECT e.id, e.ts, e.session_id, e.project, e.name, e.model, e.duration_ms,
       e.status_code status_code,
       e.error_text error,
       -- retry_duration_ms is a REAL column, so retry_ms rides straight into the
       -- payload as a number: the Duration column would sort strings otherwise.
       COALESCE(r.total_attempts, e.attempt) attempts,
       r.retry_duration_ms retry_ms,
       r.id IS NOT NULL exhausted
FROM err e LEFT JOIN pair p ON p.err_id = e.id
           LEFT JOIN rex r ON r.id = p.rex_id
UNION ALL
SELECT o.id, o.ts, o.session_id, o.project, o.name, o.model, o.duration_ms,
       o.status_code,
       o.error_text,
       o.total_attempts,
       o.retry_duration_ms,
       1
FROM rex o WHERE o.id NOT IN (SELECT rex_id FROM pair)
ORDER BY ts DESC, id DESC LIMIT %d"""
        % limit
    )
    out = []
    for row in store.query(*aggregates.windowed(sql, scope)):
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "session_id": row["session_id"],
                "project": row["project"],
                "name": row["name"],
                "model": aggregates.short_model(row["model"]) or "",
                "status_code": row["status_code"],
                "error": row["error"],
                "attempts": store.as_int(row["attempts"], 0),
                # The chain, not its last call, when the incident ran out of
                # retries: that is the time the user actually waited.
                "duration_ms": row["retry_ms"] or row["duration_ms"],
                "exhausted": bool(row["exhausted"]),
            }
        )
    return out


def decisions_stats(scope: request.Scope) -> list[dict[str, Any]]:
    """Tool-permission decisions over the window, counted per tool, decision and
    decision source, most frequent first.
    """
    return store.query_dicts(
        *aggregates.scoped(
            "tool_name, decision, dec_source, COUNT(*) decisions",
            "events WHERE name='tool_decision'" + aggregates.SCOPE_MARK,
            scope,
            group="tool_name, decision, dec_source",
            order="decisions DESC",
        )
    )


# A delegation: a Task/Agent tool result, or anything that carried an agent
# type. Named apart from the query that aggregates it, so the definition of the
# population reads on its own. The FROM onward, for `scoped`.
DELEGATION_CALLS = (
    "events WHERE name='tool_result' "
    "AND (agent_type IS NOT NULL OR tool_name IN ('Task','Agent'))"
)


def delegation_types(scope: request.Scope) -> list[dict[str, Any]]:
    """Delegations broken down per agent type, grouped in SQL over the whole window.
    No endpoint ships the delegation rows themselves -- every view of this population
    is an aggregate, so nothing here is capped and no total describes a kept slice."""
    return store.query_dicts(
        *aggregates.scoped(
            "COALESCE(agent_type,'(untyped)') agent_type, "
            "COUNT(*) calls, SUM(COALESCE(result_bytes,0)) total_bytes, "
            "SUM(CASE WHEN success='false' THEN 1 ELSE 0 END) failures, "
            "AVG(COALESCE(duration_ms,0)) avg_duration_ms",
            DELEGATION_CALLS + aggregates.SCOPE_MARK,
            scope,
            group="1",
            order="calls DESC, agent_type",
        )
    )


def subagents_stats(
    scope: request.Scope,
    limit: int = ANALYSIS_CAPS["subagents"],
) -> list[dict[str, Any]]:
    """What a `subagent_completed` reported of itself: real tokens, tool uses and
    duration, all promoted to columns. The other side of DELEGATION_CALLS."""
    out = []
    for row in store.query(
        *aggregates.scoped(
            "id, ts, session_id, project, agent_type, model, "
            "agent_tokens, agent_tools, duration_ms",
            "events WHERE name='subagent_completed'" + aggregates.SCOPE_MARK,
            scope,
            order="ts DESC",
            limit=limit,
        )
    ):
        out.append(
            {
                "id": row["id"],
                "ts": row["ts"],
                "session_id": row["session_id"],
                "project": row["project"],
                "agent_type": row["agent_type"] or "?",
                "model": aggregates.short_model(row["model"]) or "",
                "tokens": store.as_int(row["agent_tokens"], 0),
                "tools": store.as_int(row["agent_tools"], 0),
                "duration_ms": store.as_float(row["duration_ms"], 0),
            }
        )
    return out


_PROMPT_TOTALS = (
    "tools",
    "bytes",
    "compactions",
    "failures",
    "cost",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "calls",
)


def _finalize_prompt(prompt: dict[str, Any]) -> None:
    """Default a prompt's missing totals to 0 and derive its duration, in place."""
    for key in _PROMPT_TOTALS:
        prompt[key] = prompt.get(key) or 0
    for key in ("session_id", "project"):
        prompt[key] = prompt.get(key)
    # Seconds: two event timestamps apart, not an exported duration.
    prompt["duration_s"] = (prompt.get("ended_at") or prompt.get("started_at") or 0) - (
        prompt.get("started_at") or 0
    )


def prompt_stats(
    scope: request.Scope,
    limit: int = ANALYSIS_CAPS["prompts"],
) -> list[dict[str, Any]]:
    """Aggregate by prompt.id: what a single request actually triggered, newest
    first, capped. Cost comes from api_request events, which carry the same
    prompt.id.

    The turn query is the population and the only one the cap applies to: an
    api_request or a user_prompt already carries a prompt_id, so the cost and
    text queries enrich selected turns and add none of their own."""
    out: dict[str, dict[str, Any]] = {}
    for row in store.query(
        *aggregates.scoped(
            "prompt_id, MIN(ts) started_at, MAX(ts) ended_at, "
            "MAX(session_id) session_id, MAX(project) project, "
            "SUM(name='tool_result') tools, SUM(COALESCE(result_bytes,0)) bytes, "
            "SUM(name='compaction') compactions, SUM(success='false') failures",
            "events WHERE prompt_id IS NOT NULL" + aggregates.SCOPE_MARK,
            scope,
            group="prompt_id",
            # On the grouped alias, not on `ts`: a bare column beside a GROUP BY
            # reads whichever row of the turn SQLite happened to hold.
            order="started_at DESC, prompt_id",
            limit=limit,
        )
    ):
        out[row["prompt_id"]] = dict(row)
    for row in store.query(
        *aggregates.scoped(
            "prompt_id, SUM(COALESCE(cost_usd,0)) cost, "
            "SUM(COALESCE(in_tokens,0)) input_tokens, "
            "SUM(COALESCE(out_tokens,0)) output_tokens, "
            "SUM(COALESCE(cache_create,0)) cache_creation_tokens, "
            "COUNT(*) calls",
            "events WHERE name='api_request' AND prompt_id IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            group="prompt_id",
        )
    ):
        prompt = out.get(row["prompt_id"])
        if prompt is None:
            continue
        prompt.update(dict(row))
    for row in store.query(
        *aggregates.scoped(
            "prompt_id, prompt_text, ts, session_id, project",
            "events WHERE name='user_prompt' AND prompt_id IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            order="ts",
        )
    ):
        prompt = out.get(row["prompt_id"])
        if prompt is None:
            continue
        prompt["prompt_text"] = row["prompt_text"]
        prompt["session_id"] = prompt.get("session_id") or row["session_id"]
        prompt["project"] = prompt.get("project") or row["project"]
    for prompt in out.values():
        _finalize_prompt(prompt)
    return sorted(out.values(), key=lambda x: -(x.get("started_at") or 0))


def source_breakdown(scope: request.Scope) -> list[dict[str, Any]]:
    """Breakdown by `query_source`: main loop, subagent delegations, auxiliary
    calls Claude Code makes on its own. Tokens per origin, not calls per type."""
    out: dict[str, dict[str, Any]] = {}
    for row in store.query(
        *aggregates.scoped(
            "query_source, model, attr_type, SUM(value) value",
            "metric_points "
            "WHERE name='claude_code.token.usage' AND query_source IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            group="query_source, model, attr_type",
        )
    ):
        source = row["query_source"]
        entry = out.setdefault(
            source, {"source": source, "models": {}, "tokens": {}, "cost": 0}
        )
        token_type = aggregates.TOKEN_TYPES.get(row["attr_type"], row["attr_type"])
        entry["tokens"][token_type] = entry["tokens"].get(token_type, 0) + (
            row["value"] or 0
        )
        family = aggregates.short_model(row["model"]) or "?"
        entry["models"][family] = entry["models"].get(family, 0) + (row["value"] or 0)
    for row in store.query(
        *aggregates.scoped(
            "query_source, SUM(value) value",
            "metric_points "
            "WHERE name='claude_code.cost.usage' AND query_source IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            group="query_source",
        )
    ):
        source = row["query_source"]
        out.setdefault(
            source, {"source": source, "models": {}, "tokens": {}, "cost": 0}
        )
        out[source]["cost"] = row["value"] or 0
    return sorted(out.values(), key=lambda x: -x["cost"])


def api_analysis(filters: request.Filters) -> dict[str, Any]:
    """Same analyses as the session detail, at global scope.

    `truncated` names the per-call lists that hit their ANALYSIS_CAPS ceiling,
    and is always present, empty included.
    """
    scope = filters.scope()
    truncated: list[str] = []
    skills, mcp = inventory_stats(scope)
    return {
        "tools": tool_stats(scope),
        "bash": aggregates.capped(
            truncated,
            "bash",
            ANALYSIS_CAPS["bash"],
            lambda limit: bash_calls(scope, limit=limit),
        ),
        "skills": skills,
        "mcp": mcp,
        "decisions": decisions_stats(scope),
        "errors": aggregates.capped(
            truncated,
            "errors",
            ANALYSIS_CAPS["errors"],
            lambda limit: errors_calls(scope, limit=limit),
        ),
        "api_errors": aggregates.capped(
            truncated,
            "api_errors",
            ANALYSIS_CAPS["api_errors"],
            lambda limit: provider_errors(scope, limit=limit),
        ),
        "prompts": aggregates.capped(
            truncated,
            "prompts",
            ANALYSIS_CAPS["prompts"],
            lambda limit: prompt_stats(scope, limit=limit),
        ),
        # No delegation list, no origin breakdown: those belong to Costs.
        "subagents": aggregates.capped(
            truncated,
            "subagents",
            ANALYSIS_CAPS["subagents"],
            lambda limit: subagents_stats(scope, limit=limit),
        ),
        "truncated": truncated,
    }


CALLS_MAX_ROWS = 200


def api_calls(
    label: str,
    filters: request.Filters,
    session: str | None = None,
    prompt: str | None = None,
    file: str | None = None,
) -> list[dict[str, Any]]:
    """The tool_result calls behind one label, file or drill-down, newest first,
    capped at CALLS_MAX_ROWS. Each row's id opens api_event for the full detail.

    A `prompt` or `session` scopes the list to that one alone; otherwise the
    filter window applies. `label` and `file` narrow further, and `file` counts
    only Edit/Write so the row clicked and the list it opens hold the same number.
    With neither a label nor a file the list would answer the most recent calls
    of anything, so it returns empty.
    """
    # Opened from a session or a prompt, a drill-down is scoped to it alone.
    if prompt:
        scope = request.Scope(" AND prompt_id=?", (prompt,))
    elif session:
        scope = request.Scope(" AND session_id=?", (session,))
    else:
        scope = filters.scope()
    # Without either, this would answer the most recent calls of anything.
    if not label and not file:
        return []
    if label:
        scope = scope.narrow(" AND label=?", label)
    if file:
        # The population file_stats counts, so the row clicked and the list it
        # opens hold the same number.
        scope = scope.narrow(" AND file_path=? AND tool_name IN ('Edit','Write')", file)
    rows = store.query_dicts(
        *aggregates.scoped(
            "id,ts,session_id,project,label,result_bytes,duration_ms,success,"
            "error_type,bash_cmd,file_path",
            "events WHERE name='tool_result'" + aggregates.SCOPE_MARK,
            scope,
            order="ts DESC",
            limit=CALLS_MAX_ROWS,
        )
    )
    for row in rows:
        row["success"] = aggregates.success_bool(row["success"])
    return rows
