"""The vocabulary the read path shares: the metric and token names as stored,
the weights, and the small helpers every endpoint aggregates through.

Holds no endpoint. Everything here is read by more than one of them.
"""

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from . import request, store

# The OTLP `type` attribute to the name every payload carries. The one place
# the wire spelling is translated, apart from the SQL pivots.
TOKEN_TYPES = {
    "input": "input",
    "cacheRead": "cache_read",
    "cacheCreation": "cache_creation",
    "output": "output",
}

# Relative token weight (base input=1), mirroring Anthropic's pricing ratios.
# Powers the "Real token weight" view only; real $ comes from
# claude_code.cost.usage. See docs/reference.md.
WEIGHTS = {"input": 1.0, "cache_read": 0.1, "cache_creation": 1.25, "output": 5.0}

# `repl_main_thread` from the CLI, `main` from builds older than mid-2026, `sdk`
# from the Agent SDK. MAIN_THREAD_CLAUSE and the `is_main_thread` flag api_costs
# ships are both derived from this one spelling of the list.
MAIN_THREAD_ORIGINS = ("repl_main_thread", "main", "sdk")

# Inlined and not bound: the queries carrying it compose their own parameters.
MAIN_THREAD_CLAUSE = "query_origin IN (%s)" % ",".join(
    "'%s'" % origin for origin in MAIN_THREAD_ORIGINS
)

# Tool family, read from the label prefix built by make_label. Order matters:
# the first matching prefix wins, anything else is a built-in tool.
KINDS = (("mcp:", "MCP"), ("skill:", "Skill"), ("agent:", "Agent"))


# Where a query's window goes. Each occurrence takes the scope's clause, and
# `windowed` repeats the scope's args once per occurrence -- the alignment a
# nested subquery used to spell `scope.args * 2` by hand.
SCOPE_MARK = "{scope}"


def windowed(
    sql: str,
    scope: request.Scope,
    args: tuple[Any, ...] = (),
) -> tuple[str, tuple[Any, ...]]:
    """A SQL template with its window filled in, ready for a `store.query*`.

    Every `SCOPE_MARK` in `sql` is replaced by `scope.clause`, and `scope.args`
    is appended once per marker, after the query's own `args`. Pass
    `Scope.UNBOUNDED` to render the markers empty; a query that means the whole
    store with no window at all does not come through here -- it calls
    `store.query*` directly.

    Args:
        sql: The query, carrying at least one `SCOPE_MARK` marker.
        scope: The window and narrowing to render into each marker.
        args: The query's own placeholder values, which precede the window's.

    Returns:
        The rendered SQL and the full argument tuple, in placeholder order.

    Raises:
        ValueError: If `sql` carries no `SCOPE_MARK` -- a windowed helper with
            nowhere to put the window is the silent full-table scan this seam
            exists to make unrepresentable.
    """
    count = sql.count(SCOPE_MARK)
    if not count:
        raise ValueError("windowed query has no %s marker" % SCOPE_MARK)
    return sql.replace(SCOPE_MARK, scope.clause), args + scope.args * count


def scoped(
    select: str,
    population: str,
    scope: request.Scope,
    *,
    group: str = "",
    order: str = "",
    limit: int | None = None,
    args: tuple[Any, ...] = (),
) -> tuple[str, tuple[Any, ...]]:
    """A windowed `SELECT`, assembled and ready for a `store.query*`.

    `population` is the `FROM` onward -- the table and its predicate -- carrying
    a `SCOPE_MARK` wherever the window applies, so a query that forgets to bound
    itself has no marker to forget. The tail clauses are appended in SQL order.

    Args:
        select: The projection list, between `SELECT` and `FROM`.
        population: The `FROM` clause and predicate, with `SCOPE_MARK` markers.
        scope: The window and narrowing.
        group: A `GROUP BY` body, omitted when empty.
        order: An `ORDER BY` body, omitted when empty.
        limit: A `LIMIT`, formatted in since SQLite takes no LIMIT parameter.
        args: The population's own placeholder values, before the window's.

    Returns:
        The rendered SQL and the full argument tuple, in placeholder order.
    """
    sql = "SELECT " + select + " FROM " + population
    if group:
        sql += " GROUP BY " + group
    if order:
        sql += " ORDER BY " + order
    if limit is not None:
        sql += " LIMIT %d" % limit
    return windowed(sql, scope, args)


def short_model(model: str | None) -> str | None:
    """The model family, capitalised, from a full model id: `Opus`, `Sonnet`,
    `Haiku`, `Fable` or `Mythos`. An unrecognised id keeps its first `-` segment,
    and a blank or missing id returns None -- a family that sorts against nothing.
    """
    if not model:
        return None
    model = model.replace("claude-", "")
    for family in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if family in model:
            return family.capitalize()
    return model.split("-")[0].capitalize()


_SUCCESS_BY_WIRE = {"true": True, "false": False}


def success_bool(value: str | None) -> bool | None:
    """The `tool_result` success flag, typed at the serve boundary.

    OTEL sends the string `"true"` / `"false"`; this returns the matching bool.
    Any other value -- most of all the NULL a non-tool event carries -- stays
    None, no success either way.
    """
    if value is None:
        return None
    return _SUCCESS_BY_WIRE.get(value)


# SPENT_SESSIONS names the sessions that did spend; IDLE_SESSIONS names the ones
# positively known not to have, which is what a query over `events` needs. Both
# carry a SCOPE_MARK, so a query nesting one is windowed by `scoped`/`windowed`.
SPEND_METRICS = "('claude_code.token.usage','claude_code.cost.usage')"
SPENT_SESSIONS = (
    "SELECT mp.session_id FROM metric_points mp WHERE mp.name IN "
    + SPEND_METRICS
    + SCOPE_MARK
    + " GROUP BY mp.session_id HAVING SUM(mp.value)>0"
)
IDLE_SESSIONS = (
    "SELECT mp.session_id FROM metric_points mp "
    "WHERE mp.session_id IS NOT NULL" + SCOPE_MARK + " GROUP BY mp.session_id "
    "HAVING SUM(CASE WHEN mp.name IN " + SPEND_METRICS + " THEN mp.value ELSE 0 END)=0"
)

# The aggregated columns of a session, shared by the list and by the single
# session, so both payloads carry the same keys. 'cacheRead' and 'cacheCreation'
# are the OTLP spelling as stored; the aliases are the canonical names.
SESSION_TOTALS = """
        SUM(CASE WHEN name='claude_code.cost.usage' THEN value END) cost,
        SUM(CASE WHEN name='claude_code.token.usage' AND attr_type='input'
                 THEN value END) input_tokens,
        SUM(CASE WHEN name='claude_code.token.usage' AND attr_type='cacheRead'
                 THEN value END) cache_read_tokens,
        SUM(CASE WHEN name='claude_code.token.usage' AND attr_type='cacheCreation'
                 THEN value END) cache_creation_tokens,
        SUM(CASE WHEN name='claude_code.token.usage' AND attr_type='output'
                 THEN value END) output_tokens,
        SUM(CASE WHEN name='claude_code.lines_of_code.count' AND attr_type='added'
                 THEN value END) lines_added,
        SUM(CASE WHEN name='claude_code.lines_of_code.count' AND attr_type='removed'
                 THEN value END) lines_removed,
        SUM(CASE WHEN name='claude_code.active_time.total' THEN value END) active_seconds"""

# The aliases SESSION_TOTALS produces. A missing metric lands a NULL, which
# sorts as a string, so every caller zeroes them by this tuple.
SESSION_SUMS = (
    "cost",
    "input_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "output_tokens",
    "lines_added",
    "lines_removed",
    "active_seconds",
)


def attrs_of(row: sqlite3.Row, column: str = "attrs") -> dict[str, Any]:
    """The raw attribute blob of a stored row, back as a dict: everything the
    ingester did not promote to a column. An unparsable blob raises -- it would
    mean the ingester wrote something that is not JSON."""
    return json.loads(store.attrs_json(row[column]))


# The name a hook is grouped and looked up under, as SQL. Claude Code exports
# `hook_name` ("PreToolUse:Bash") only when the matcher is known, so it falls
# back to `hook_event`. Both are columns since #179, indexable and no longer a
# json.loads per row over the whole history.
HOOK_KEY = "COALESCE(hook_name,hook_event,'?')"

# The three numbers a fire carries, zero when it exported none. The columns are
# typed (REAL/INTEGER), so "52" no longer sorts above "3791" as it did in text.
HOOK_MS = "COALESCE(hook_duration_ms,0)"
HOOK_ERR = "COALESCE(hook_err,0)"
HOOK_BLOCK = "COALESCE(hook_block,0)"


def tokens_by_type(scope: request.Scope) -> dict[str, Any]:
    """Tokens per type, keyed by the canonical name: the one place /api/overview
    and /api/costs leave the OTLP spelling. An unknown type passes through."""
    return {
        TOKEN_TYPES.get(row["attr_type"], row["attr_type"]): row["value"] or 0
        for row in store.query(
            *scoped(
                "attr_type, SUM(value) value",
                "metric_points WHERE name='claude_code.token.usage'" + SCOPE_MARK,
                scope,
                group="attr_type",
            )
        )
        if row["attr_type"]
    }


def weighted_tokens(tokens: Mapping[str, Any], suffix: str = "") -> dict[str, float]:
    """Each token count times its WEIGHTS ratio, keyed by type. `suffix` names the
    spelling a session row carries the counts under (`input_tokens`)."""
    return {
        token_type: (tokens.get(token_type + suffix) or 0) * weight
        for token_type, weight in WEIGHTS.items()
    }


def capped[T](
    truncated: list[str],
    name: str,
    ceiling: int,
    produce: Callable[[int], list[T]],
) -> list[T]:
    """Ask for one row past the ceiling: that is what tells a list that ends there
    from one that merely fills it. Appends `name` to `truncated` when it was cut."""
    out = produce(ceiling + 1)
    if len(out) <= ceiling:
        return out
    truncated.append(name)
    return out[:ceiling]
