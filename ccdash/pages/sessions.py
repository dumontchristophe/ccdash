"""A session, listed and opened: the titles, the per-session figures, and the
three endpoints that ship them -- /api/sessions, /api/session, /api/context.
"""

import re
import statistics
from collections.abc import Callable, Sequence
from typing import Any

from ..core import aggregates, request, store
from . import analysis

RENAME_RE = re.compile(r"^\s*/rename\s+(.+)$", re.I)

# A session title is one line in a list, so it is cut to what that line shows.
TITLE_MAX_CHARS = 120


def session_titles(scope: request.Scope) -> dict[str, dict[str, str]]:
    """A /rename command caught in a user_prompt, otherwise the session's first
    prompt. Both require OTEL_LOG_USER_PROMPTS=1. `id` breaks the tie, `ts`
    being a second."""
    out = {}
    for row in store.query(
        *aggregates.scoped(
            "session_id, prompt_text, ts, id",
            "events WHERE name='user_prompt' AND prompt_text IS NOT NULL"
            + aggregates.SCOPE_MARK,
            scope,
            order="ts, id",
        )
    ):
        text = row["prompt_text"] or ""
        rename_match = RENAME_RE.match(text)
        if rename_match:
            out[row["session_id"]] = {
                "title": rename_match.group(1).strip()[:TITLE_MAX_CHARS],
                "src": "rename",
            }
        elif row["session_id"] not in out:
            out[row["session_id"]] = {
                "title": " ".join(text.split())[:TITLE_MAX_CHARS],
                "src": "prompt",
            }
    return out


def session_models(session_ids: Sequence[str]) -> dict[str, set[str | None]]:
    """The model chips of a page of sessions, in one query. query_source='main'
    only, so the auxiliary Haiku calls do not chip every session."""
    out: dict[str, set[str | None]] = {}
    if not session_ids:
        return out
    placeholders = ",".join("?" * len(session_ids))
    for row in store.query(
        "SELECT DISTINCT session_id, model FROM metric_points "
        "WHERE model IS NOT NULL AND query_source='main' "
        "AND session_id IN (%s)" % placeholders,
        session_ids,
    ):
        out.setdefault(row["session_id"], set()).add(
            aggregates.short_model(row["model"])
        )
    return out


def session_figures(
    session_row: dict[str, Any],
    event_counts: dict[str, Any],
) -> None:
    """Zero the NULLs of one session row and derive what the list shows.
    `event_counts` is that session's event counts, empty when it produced none."""
    session_row["tools"] = event_counts.get("tools") or 0
    session_row["compactions"] = event_counts.get("compactions") or 0
    session_row["prompts"] = event_counts.get("prompts") or 0
    for key in aggregates.SESSION_SUMS:
        session_row[key] = session_row[key] or 0
    session_row["tokens"] = (
        session_row["input_tokens"]
        + session_row["cache_read_tokens"]
        + session_row["cache_creation_tokens"]
        + session_row["output_tokens"]
    )
    # What share of the session's weight went into producing: the weight
    # reproduces billing, so that is the cost spent writing rather than reading
    # the context back. Raw counts would measure cache expiry instead.
    weighted = aggregates.weighted_tokens(session_row, "_tokens")
    weighted_total = sum(weighted.values())
    session_row["output_weight_pct"] = (
        (100 * weighted["output"] / weighted_total) if weighted_total else 0
    )


def api_sessions(filters: request.Filters) -> dict[str, Any]:
    scope = filters.scope()
    out = store.query_dicts(
        *aggregates.scoped(
            "session_id, host, project, MIN(ts) started_at, MAX(ts) ended_at,"
            + aggregates.SESSION_TOTALS,
            "metric_points WHERE session_id IS NOT NULL" + aggregates.SCOPE_MARK,
            scope,
            group="session_id",
            order="ended_at DESC",
        )
    )
    counts_by_session = {
        row["session_id"]: dict(row)
        for row in store.query(
            *aggregates.scoped(
                "session_id, SUM(name='tool_result') tools, "
                "SUM(name='compaction') compactions, "
                "SUM(name='user_prompt') prompts",
                "events WHERE session_id IS NOT NULL" + aggregates.SCOPE_MARK,
                scope,
                group="session_id",
            )
        )
    }
    for session in out:
        session_figures(session, counts_by_session.get(session["session_id"]) or {})
    # No subquery here: the rows already carry the sums the idle test needs, so
    # the same rule SPENT_SESSIONS states in SQL costs one pass in Python here.
    spent = [session for session in out if session["tokens"] or session["cost"]]
    idle = len(out) - len(spent)
    out = spent
    # `output_weight_pct` is a ratio, so its median is the middle session's
    # share, not the summed weights'.
    median = {"sessions": len(out), "idle": idle}
    for key in ("active_seconds", "prompts", "tokens", "cost", "output_weight_pct"):
        median[key] = statistics.median([session[key] for session in out]) if out else 0
    titles = session_titles(scope)
    models_by_session = session_models([session["session_id"] for session in out])
    for session in out:
        title = titles.get(session["session_id"]) or {}
        session["title"] = title.get("title")
        session["title_src"] = title.get("src")
        session["models"] = sorted(models_by_session.get(session["session_id"], set()))
    return {"sessions": out, "median": median}


# Not a page size: a session detail already has one session for a window, so this
# guards against a runaway session freezing the tab. It sits high enough that no
# ordinary session reaches it, and whatever it cuts is named in `truncated`.
SESSION_MAX_ROWS = 10000

# How much of an assistant response a timeline row carries. `response_length`
# rides along so a row can tell a text that was cut from one that ends there.
RESPONSE_CLIP = 300


def _session_span(session_id: str) -> tuple[int, int] | None:
    """First and last timestamp recorded under a session id, None when nothing
    carries it.

    Both tables, not just the metric points the head aggregates read: a session
    that registered a hook and stopped there has events and no metric point, and
    it is still a session the dashboard holds rows for.
    """
    row = store.query_row(
        "SELECT MIN(ts) started_at, MAX(ts) ended_at FROM ("
        "SELECT ts FROM metric_points WHERE session_id=? "
        "UNION ALL SELECT ts FROM events WHERE session_id=?)",
        (session_id, session_id),
    )
    if row is None or row["started_at"] is None:
        return None
    return row["started_at"], row["ended_at"]


def _session_head(session_id: str, span: tuple[int, int]) -> dict[str, Any]:
    """The header figures of one session: totals with their NULLs zeroed, the
    weighting the bar is drawn from, and the chips.

    Args:
        session_id: The session the header describes.
        span: Its first and last timestamp, used where the metric points are
          silent.
    """
    head_row = store.query_row(
        "SELECT MIN(ts) started_at, MAX(ts) ended_at, host, project,"
        + aggregates.SESSION_TOTALS
        + " FROM metric_points WHERE session_id=?",
        (session_id,),
    )
    head = dict(head_row) if head_row else {}
    for key in aggregates.SESSION_SUMS:
        head[key] = head.get(key) or 0
    # SESSION_TOTALS covers metric points alone: a session with events and none
    # of them leaves both NULL, which the header prints as Jan 1970.
    head["started_at"] = head.get("started_at") or span[0]
    head["ended_at"] = head.get("ended_at") or span[1]
    # The same weighting the Costs page ships (api_costs), so the two bars mean
    # the same thing. Raw counts would make every session a solid cache_read
    # block: re-reading dominates the volume while it weighs 0.1.
    head["weights"] = aggregates.WEIGHTS
    head["weighted"] = aggregates.weighted_tokens(head, "_tokens")
    head.update(_head_chips(session_id))
    return head


def _head_chips(session_id: str) -> dict[str, list[str]]:
    """Which models, output styles, efforts, terminals and CLI versions the
    session ran under. query_source='main' for the models, like api_sessions:
    the auxiliary Haiku calls would otherwise show on nearly every session."""
    models: set[str | None] = {
        aggregates.short_model(row["model"])
        for row in store.query(
            "SELECT DISTINCT model FROM metric_points WHERE session_id=? AND model IS NOT NULL "
            "AND query_source='main'",
            (session_id,),
        )
        if row["model"]
    }
    return {
        # short_model only returns None on a model the query already excludes.
        "models": sorted(model for model in models if model is not None),
        # Distinct, not the last: a session that switched mid-run shows both.
        "output_styles": _request_values(session_id, "output_style"),
        "efforts": _request_values(session_id, "effort"),
        "terminals": _first_seen_values(session_id, "terminal_type"),
        "versions": _first_seen_values(session_id, "service_version"),
    }


# The alias both chip queries select into. Not a column of `events`, and named
# so it never becomes one: SQLite would bind the WHERE and the GROUP BY to the
# real column instead, silently.
CHIP_ALIAS = "chip_value"


def _request_values(session_id: str, column: str) -> list[str]:
    """The distinct values one api_request column took over a session, sorted."""
    return [
        row[CHIP_ALIAS]
        for row in store.query(
            "SELECT DISTINCT " + column + " " + CHIP_ALIAS + " FROM events "
            "WHERE session_id=? AND name='api_request' "
            "AND " + CHIP_ALIAS + " IS NOT NULL ORDER BY 1",
            (session_id,),
        )
    ]


def _first_seen_values(session_id: str, column: str) -> list[str]:
    """The distinct values of one column, in the order they first appeared.

    Not sorted by value: 2.1.76 sorts after 2.1.237 as text. Read off every kind
    of record, since these columns are not reliably on api_request."""
    return [
        row[CHIP_ALIAS]
        for row in store.query(
            "SELECT " + column + " " + CHIP_ALIAS + ", "
            "MIN(ts) first_ts, MIN(id) first_id FROM events WHERE session_id=? "
            "AND " + CHIP_ALIAS + " IS NOT NULL "
            "GROUP BY " + CHIP_ALIAS + " ORDER BY first_ts, first_id",
            (session_id,),
        )
    ]


def _timeline_events(session_id: str, truncated: list[str]) -> list[dict[str, Any]]:
    """The session's records, newest first, capped at SESSION_MAX_ROWS.

    `response` is clipped rather than shipped whole, which would be megabytes per
    row. `hook_execution_start` is dropped -- the matching
    `hook_execution_complete` carries the same attributes plus the result -- in
    SQL, so the cap applies to what is shown.

    Args:
        session_id: The session the timeline covers.
        truncated: Collects "events" when the cap cut the list.
    """
    rows = aggregates.capped(
        truncated,
        "events",
        SESSION_MAX_ROWS,
        lambda limit: store.query_dicts(
            "SELECT id,ts,name,label,tool_name,success,duration_ms,result_bytes,"
            "error_type,bash_cmd,file_path,trigger_kind,pre_tokens,post_tokens,decision,"
            "dec_source,skill_name,prompt_text,agent_type,agent_desc,prompt_id,model,"
            "COALESCE(hook_name,hook_event) hook_name,"
            "substr(response,1,%d) response,response_length,from_mode,to_mode,"
            # The aliases stay clear of the real columns: `mcp_name` because
            # `mcp_server` is one, `error_msg` because `api_error` is an event
            # name.
            "mcp_status,server_name mcp_name,transport_type mcp_transport,"
            "hook_duration_ms hook_ms,error_name,status_code,error_text error_msg,"
            "COALESCE(total_attempts,attempt) attempts,retry_duration_ms retry_ms,"
            "mention_type "
            "FROM events WHERE name <> 'hook_execution_start' AND session_id=? "
            "ORDER BY ts DESC, id DESC LIMIT %d" % (RESPONSE_CLIP, limit),
            (session_id,),
        ),
    )
    for row in rows:
        row["success"] = aggregates.success_bool(row["success"])
    return rows


def _session_listings(scope: request.Scope, truncated: list[str]) -> dict[str, Any]:
    """The per-tab listings of one session, each capped at SESSION_MAX_ROWS.

    Args:
        scope: The SQL scope naming the session.
        truncated: Collects the name of every listing the cap cut.
    """

    def capped(
        name: str,
        produce: Callable[[request.Scope, int], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        return aggregates.capped(
            truncated,
            name,
            SESSION_MAX_ROWS,
            lambda limit: produce(scope, limit),
        )

    # Bound to locals: `truncated` names the lists in read order, not payload
    # order.
    bash = capped("bash", analysis.bash_calls)
    files = capped("files", analysis.file_stats)
    subagents = capped("subagents", analysis.subagents_stats)
    errors = capped("errors", analysis.errors_calls)
    api_errors = capped("api_errors", analysis.provider_errors)
    prompts = capped("prompts", analysis.prompt_stats)
    skills, mcp = analysis.inventory_stats(scope)
    return {
        "sources": analysis.source_breakdown(scope),
        "tools": analysis.tool_stats(scope),
        "bash": bash,
        "files": files,
        "prompts": prompts,
        "subagents": subagents,
        "skills": skills,
        "mcp": mcp,
        "errors": errors,
        "api_errors": api_errors,
        "decisions": analysis.decisions_stats(scope),
    }


def _session_compactions(session_id: str) -> list[dict[str, Any]]:
    """Every compaction the session went through, oldest first."""
    return store.query_dicts(
        "SELECT ts,trigger_kind,pre_tokens,post_tokens FROM events "
        "WHERE session_id=? AND name='compaction' ORDER BY ts",
        (session_id,),
    )


def _context_curve(session_id: str) -> list[dict[str, Any]]:
    """The size of the prompt sent by each main-thread call, in order.

    Every input token is fresh, read from the cache or written to it, so the
    three summed are the prompt sent. Sub-agents hold their own context."""
    return store.query_dicts(
        "SELECT ts, COALESCE(in_tokens,0)+COALESCE(cache_read,0)"
        "+COALESCE(cache_create,0) value FROM events WHERE session_id=? "
        "AND name='api_request' AND " + aggregates.MAIN_THREAD_CLAUSE + " ORDER BY ts",
        (session_id,),
    )


def api_session(session_id: str) -> dict[str, Any]:
    """Everything one session carries: the head figures, the timeline, and each
    per-row listing the tabs read.

    Raises:
        request.NotFoundError: If no row carries this session id.
    """
    span = _session_span(session_id)
    if span is None:
        raise request.NotFoundError(session_id)
    scope = request.Scope(" AND session_id=?", (session_id,))
    head = _session_head(session_id, span)
    title = session_titles(scope).get(session_id) or {}
    head["title"] = title.get("title")
    head["title_src"] = title.get("src")
    truncated: list[str] = []
    # Read before the listings, and placed after them: `truncated` names the
    # timeline first, while the payload keeps the key order the tabs read.
    events = _timeline_events(session_id, truncated)
    return {
        "session": session_id,
        "head": head,
        **_session_listings(scope, truncated),
        "events": events,
        "compactions": _session_compactions(session_id),
        "context": _context_curve(session_id),
        "truncated": truncated,
    }


def api_context(filters: request.Filters) -> dict[str, Any]:
    """Sessions ranked by how hard their context pressed.

    An auto compaction is the context overflowing, a manual one a choice, so
    they are counted apart (`auto_compactions` / `manual_compactions`).
    `max_context` is the largest main-thread prompt -- the only column that
    describes a session that ran hot without compacting."""
    scope = filters.scope()
    out = store.query_dicts(
        *aggregates.scoped(
            "session_id, project, MAX(ts) ts, COUNT(*) events, "
            # CASE, not the SUM(name='x') shorthand: `1 AND NULL` is NULL, so one
            # compaction with no trigger_kind would null the whole sum.
            "SUM(CASE WHEN name='compaction' AND trigger_kind='auto' "
            "         THEN 1 ELSE 0 END) auto_comp, "
            "SUM(CASE WHEN name='compaction' AND COALESCE(trigger_kind,'')<>'auto' "
            "         THEN 1 ELSE 0 END) man_comp, "
            "MAX(CASE WHEN name='compaction' THEN pre_tokens END) pre_compaction_peak, "
            "MAX(CASE WHEN name='api_request' AND "
            + aggregates.MAIN_THREAD_CLAUSE
            + " "
            "         THEN COALESCE(in_tokens,0)+COALESCE(cache_read,0)"
            "              +COALESCE(cache_create,0) END) max_context, "
            "SUM(CASE WHEN name='api_request' THEN cost_usd END) cost, "
            "SUM(CASE WHEN name='tool_result' THEN 1 ELSE 0 END) tools, "
            "SUM(CASE WHEN name='user_prompt' THEN 1 ELSE 0 END) prompts",
            # Two markers -- the outer window and the idle-session subquery --
            # so `scoped` passes the scope args twice, aligned.
            "events WHERE session_id IS NOT NULL"
            + aggregates.SCOPE_MARK
            + " AND session_id NOT IN ("
            + aggregates.IDLE_SESSIONS
            + ")",
            scope,
            group="session_id",
            order="auto_comp DESC, pre_compaction_peak DESC",
        )
    )
    for session in out:
        # Zero rather than NULL: the table sorts numbers numerically and
        # everything else as a string, so one None makes the ranking
        # lexicographic.
        session["pre_compaction_peak"] = session["pre_compaction_peak"] or 0
        session["max_context"] = session["max_context"] or 0
        session["cost"] = session["cost"] or 0
        session["tools_per_prompt"] = (
            (session["tools"] / session["prompts"]) if session["prompts"] else 0
        )
    return {
        "sessions": out,
        "auto_compactions": sum(session["auto_comp"] for session in out),
        "manual_compactions": sum(session["man_comp"] for session in out),
        "pre_compaction_peak": max(
            [session["pre_compaction_peak"] for session in out] or [0]
        ),
    }
