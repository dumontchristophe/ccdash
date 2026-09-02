"""One record, opened: an event, a completed sub-agent, a prompt turn."""

import json
import sqlite3
from typing import Any

from ..core import aggregates, request, store
from . import analysis


def api_event(event_id: int) -> dict[str, Any]:
    """Inspector: all raw attributes of an event, unfiltered."""
    row = store.query_row(
        "SELECT events.*, event_attrs.attrs AS raw_attrs FROM events "
        "LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
        "WHERE events.id=?",
        (event_id,),
    )
    if not row:
        return {"error": request.NOT_FOUND}
    event = dict(row)
    # The blob lives in the sibling now, written zlib-compressed (#181); the
    # deprecated events.attrs column it left behind (NULL on every new row) is
    # overwritten by it here. store.attrs_json inflates it, None stays None.
    raw_attrs = event.pop("raw_attrs")
    event["attrs"] = store.attrs_json(raw_attrs) if raw_attrs else None
    for column in ("attrs", "params"):
        if event.get(column):
            try:
                event[column] = json.loads(event[column])
            except ValueError:
                pass
    # The arguments the call was made with, normalised here rather than in the
    # browser: the Edit and Write panels read `old_string` / `content` out of it.
    event["tool_input"] = (
        store.tool_input(event["attrs"])
        if isinstance(event.get("attrs"), dict)
        else None
    )
    event["success"] = aggregates.success_bool(event["success"])
    return event


def api_subagent(event_id: int) -> dict[str, Any]:
    """Real metrics from the subagent_completed event, plus the instructions it
    was given, joined from the spawning Agent/Task tool_result by prompt_id. Its
    own tool calls share the parent prompt_id, so they are not attributable."""
    row = store.query_row(
        "SELECT events.ts, events.session_id, events.prompt_id, event_attrs.attrs "
        "FROM events LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
        "WHERE events.id=? AND events.name='subagent_completed'",
        (event_id,),
    )
    if not row:
        return {"error": request.NOT_FOUND}
    attrs = aggregates.attrs_of(row)
    out = {
        "ts": row["ts"],
        "session": row["session_id"],
        "agent_type": attrs.get("agent_type") or "?",
        "model": aggregates.short_model(attrs.get("model")) or "",
        "tokens": store.as_int(attrs.get("total_tokens"), 0),
        "tools": store.as_int(attrs.get("total_tool_uses"), 0),
        "duration_ms": store.as_float(attrs.get("duration_ms"), 0),
        "background": attrs.get("is_async"),
        "isolation": None,
        "description": None,
        "instructions": None,
    }
    prompt_id = attrs.get("prompt.id") or row["prompt_id"]
    out["efforts"] = _subagent_efforts(
        row["session_id"], prompt_id, out["agent_type"], attrs.get("is_built_in")
    )
    if prompt_id:
        out.update(_spawn_instructions(prompt_id, out["agent_type"]))
    return out


def _subagent_efforts(
    session_id: str,
    prompt_id: str | None,
    agent_type: str,
    built_in: Any,
) -> list[Any]:
    """The distinct `effort` of the api_requests this sub-agent made -- the
    completion event carries none. Agents of one type under one prompt share
    those rows, so the list then covers all of them."""
    if not prompt_id:
        return []
    origins = (
        "agent:" + agent_type,
        "agent:builtin:" + agent_type,
        "agent:custom" if not built_in else "agent:" + agent_type,
    )
    return [
        row["effort"]
        for row in store.query(
            "SELECT DISTINCT effort FROM events "
            "WHERE session_id=? AND prompt_id=? AND name='api_request' "
            "AND query_origin IN (?,?,?) AND effort IS NOT NULL ORDER BY 1",
            (session_id, prompt_id) + origins,
        )
    ]


def _spawn_instructions(prompt_id: str, agent_type: str) -> dict[str, Any]:
    """Instructions read from the Task/Agent tool_result that spawned the
    sub-agent. One prompt can spawn several: the last read wins, unless one
    announces the expected type. `background` survives a candidate omitting it."""
    found: dict[str, Any] = {}
    for task in store.query_dicts(
        "SELECT event_attrs.attrs FROM events "
        "LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
        "WHERE events.name='tool_result' AND events.tool_name IN ('Task','Agent') "
        "AND events.prompt_id=? ORDER BY events.ts",
        (prompt_id,),
    ):
        tool_input = store.tool_input(task["attrs"])
        if tool_input is None:
            continue
        found.update(
            {
                "description": tool_input.get("description"),
                "instructions": tool_input.get("prompt"),
                "isolation": tool_input.get("isolation"),
            }
        )
        if tool_input.get("run_in_background") is not None:
            found["background"] = tool_input.get("run_in_background")
        if tool_input.get("subagent_type") == agent_type:
            break
    return found


def api_prompt(prompt_id: str) -> dict[str, Any]:
    """Everything one prompt set off, scoped on prompt_id: model calls, tools,
    sub-agents, hooks. Claude Code stamps every event of a turn with the same
    prompt.id, so the usual helpers take that scope unchanged. The text is shown
    whole -- user_prompt escapes the tool_input sanitiser (docs/reference.md)."""
    scope = request.Scope(" AND prompt_id=?", (prompt_id,))
    # A turn logged without its user_prompt (OTEL_LOG_USER_PROMPTS off) still
    # has a span, hence `{}` rather than a None to test on every field.
    head = dict(
        store.query_row(
            "SELECT ts, session_id, project, prompt_text FROM events "
            "WHERE name='user_prompt' AND prompt_id=? ORDER BY ts LIMIT 1",
            (prompt_id,),
        )
        or {}
    )
    # Fallbacks for a turn logged without its user_prompt event, plus the span the
    # duration is read from.
    span = store.query_row(
        "SELECT MIN(ts) started_at, MAX(ts) ended_at, MAX(session_id) session_id, "
        "MAX(project) project FROM events WHERE prompt_id=?",
        (prompt_id,),
    )
    if not span or span["started_at"] is None:
        return {"error": request.NOT_FOUND}
    # An aggregate with no GROUP BY returns a row whatever the table holds.
    api_totals: sqlite3.Row = store.query_row(  # type: ignore[assignment]
        "SELECT COUNT(*) calls, COALESCE(SUM(cost_usd),0) cost, "
        "COALESCE(SUM(in_tokens),0) input_tokens, "
        "COALESCE(SUM(out_tokens),0) output_tokens, "
        "COALESCE(SUM(cache_read),0) cache_read_tokens, "
        "COALESCE(SUM(cache_create),0) cache_creation_tokens "
        "FROM events WHERE name='api_request' AND prompt_id=?",
        (prompt_id,),
    )
    # Hook overhead per hook, grouped over the turn's fires.
    hooks = store.query_dicts(
        *aggregates.scoped(
            aggregates.HOOK_KEY
            + " name, COUNT(*) fires, SUM("
            + aggregates.HOOK_MS
            + ") ms",
            "events WHERE events.name='hook_execution_complete'"
            + aggregates.SCOPE_MARK,
            scope,
            group="1",
            order="ms DESC, 1",
        )
    )
    hook_ms = sum(hook["ms"] for hook in hooks)
    return {
        "id": prompt_id,
        "ts": head.get("ts") or span["started_at"],
        "session_id": head.get("session_id") or span["session_id"],
        "project": head.get("project") or span["project"],
        "prompt_text": head.get("prompt_text"),
        # Seconds: two event timestamps apart, not an exported duration.
        "duration_s": span["ended_at"] - span["started_at"],
        "calls": api_totals["calls"],
        "cost": api_totals["cost"],
        "input_tokens": api_totals["input_tokens"],
        "output_tokens": api_totals["output_tokens"],
        "cache_read_tokens": api_totals["cache_read_tokens"],
        "cache_creation_tokens": api_totals["cache_creation_tokens"],
        "hook_ms": hook_ms,
        "hooks": hooks,
        # Per tool, with its own failure count: the totals say less.
        "toolstats": analysis.tool_stats(scope),
        "subagents": analysis.subagents_stats(scope),
    }
