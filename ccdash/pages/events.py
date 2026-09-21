"""Event rows across the window, one session boundary ignored: /api/events, and
the row projection it shares with the session timeline.
"""

from typing import Any

from ..core import aggregates, request

# How much of an assistant response an event row carries. `response_length`
# rides along so a row can tell a text that was cut from one that ends there.
RESPONSE_CLIP = 300

# An event row as every payload ships it: the session timeline selects this and
# /api/events this plus `session_id` and `project`, so the two cannot drift.
# `response` is clipped rather than shipped whole, which would be megabytes per
# row.
EVENT_COLUMNS = (
    "id,ts,name,label,tool_name,success,duration_ms,result_bytes,"
    "error_type,bash_cmd,file_path,trigger_kind,pre_tokens,post_tokens,decision,"
    "dec_source,skill_name,prompt_text,agent_type,agent_desc,prompt_id,model,"
    "COALESCE(hook_name,hook_event) hook_name,"
    "substr(response,1,%d) response,response_length,from_mode,to_mode,"
    # The aliases stay clear of the real columns: `mcp_name` because
    # `mcp_server` is one, `error_msg` because `api_error` is an event name.
    "mcp_status,server_name mcp_name,transport_type mcp_transport,"
    "hook_duration_ms hook_ms,error_name,status_code,error_text error_msg,"
    "COALESCE(total_attempts,attempt) attempts,retry_duration_ms retry_ms,"
    "mention_type" % RESPONSE_CLIP
)

# The single-value filters, query parameter to column. `name` is not here: it
# repeats, and matches any of its values.
EVENT_FILTERS = {"label": "label", "skill": "skill_name", "session": "session_id"}

# This route answers a script, not a screen, so it defaults higher than the
# display lists do.
EVENTS_PER_PAGE = 500


def api_events(
    params: dict[str, list[str]],
    filters: request.Filters,
) -> dict[str, Any]:
    """One page of the window's events, newest first, `id` breaking the tie.

    No event name is excluded: the timeline's drop of `hook_execution_start`
    is its own predicate, and a caller asking for that name gets its rows. With
    no event filter the page is the window's most recent events.

    Raises:
        BadRequestError: If `page`, `per_page` or a window parameter is
            unreadable.
    """
    scope = filters.scope()
    names = params.get("name") or []
    if names:
        scope = scope.narrow(" AND name IN (%s)" % ",".join("?" * len(names)), *names)
    for key, column in EVENT_FILTERS.items():
        value = request.one_param(params, key)
        if value:
            scope = scope.narrow(" AND %s=?" % column, value)
    envelope = aggregates.paginated(
        EVENT_COLUMNS + ",session_id,project",
        "events WHERE 1" + aggregates.SCOPE_MARK,
        scope,
        request.Page.from_params(params, EVENTS_PER_PAGE),
        order="ts DESC, id DESC",
    )
    for row in envelope["data"]:
        row["success"] = aggregates.success_bool(row["success"])
    return envelope
