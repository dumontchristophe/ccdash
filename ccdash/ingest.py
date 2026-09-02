"""OTLP write path: the decoders that turn an exported payload into rows, the
two ingesters that store them, and the transport limits the handler reads a body
under.
"""

import io
import json
import sys
import zlib
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

from .core import store

# What an OTLP AnyValue decodes to once the wrapper is gone. Nothing downstream
# reads a typed field back, so every consumer handles the whole union.
type JsonValue = str | bool | int | float | list[Any] | dict[str, Any] | None

DROP_ATTRS = {
    "user.email",
    "user.id",
    "user.account_uuid",
    "user.account_id",
    "organization.id",
    "user.groups",
}

# An oversized body or a decompression bomb must not take the process RAM.
MAX_BODY = 32 * 1024 * 1024  # 32 MB received (compressed)
MAX_DECOMPRESSED = 128 * 1024 * 1024  # 128 MB after decompression
CHUNK_LINE_MAX = 4096  # a chunk size line, extensions included


def anyvalue(v: Any) -> JsonValue:
    """One OTLP `AnyValue` decoded to its plain Python value.

    Unwraps the typed wrapper OTLP boxes every attribute in -- `stringValue`,
    `intValue`, `doubleValue`, and so on -- recursing through `arrayValue` and
    `kvlistValue`. A non-dict is already plain and passes through; an int or
    double that will not parse keeps its raw form, and an unrecognised shape
    decodes to None.
    """
    if not isinstance(v, dict):
        return v
    for k in ("stringValue", "boolValue", "bytesValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        try:
            return int(v["intValue"])
        except (TypeError, ValueError):
            return v["intValue"]
    if "doubleValue" in v:
        try:
            return float(v["doubleValue"])
        except (TypeError, ValueError):
            return v["doubleValue"]
    if "arrayValue" in v:
        return [anyvalue(x) for x in (v["arrayValue"].get("values") or [])]
    if "kvlistValue" in v:
        return kvlist(v["kvlistValue"].get("values") or [])
    return None


def kvlist(items: list[dict[str, Any]] | None) -> dict[str, JsonValue]:
    """An OTLP key/value list decoded to a dict, each value through `anyvalue`.

    A key named in `DROP_ATTRS` is dropped, so the identifying attributes never
    reach storage (the whole of the stripping -- see the module docstring). A key
    that is empty or missing is skipped, and `None` reads as no items.
    """
    out: dict[str, JsonValue] = {}
    for it in items or []:
        k = it.get("key")
        if k and k not in DROP_ATTRS:
            out[k] = anyvalue(it.get("value"))
    return out


def nano_to_s(v: Any) -> int:
    """Converts an OTLP nanosecond timestamp to epoch seconds.

    A value that cannot be read -- absent as much as malformed -- falls back to
    now, and says so on stderr: the row is stored with a substitute time, which
    a reader would otherwise take for the exporter's own."""
    try:
        return int(int(v) // 1_000_000_000)
    except (TypeError, ValueError):
        sys.stderr.write("ingest: unusable timestamp %r, stored as now\n" % (v,))
        return int(datetime.now(timezone.utc).timestamp())


def _scalar(v: Any) -> Any:
    """Blanks a value sqlite refuses to bind.

    An OTLP attribute can decode to a dict (kvlistValue) or a list (arrayValue).
    Both ingesters insert through `executemany`, where one such value fails the
    whole batch. The raw value stays readable in `attrs`."""
    return None if isinstance(v, (dict, list)) else v


def parse_params(attrs: dict[str, Any]) -> dict[str, Any]:
    raw = attrs.get("tool_parameters")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return {}


def make_label(tool_name: Any, params: dict[str, Any]) -> str | None:
    """MCP servers arrive under the generic name 'mcp_tool'; the real name
    lives in tool_parameters. Returns None for a record that is not a tool call
    at all -- a hook fire, an API request, a prompt -- because the column names
    the tool and those have none. A placeholder there would be indexed, grouped
    and displayed as though it were one."""
    if params.get("mcp_server_name"):
        return "mcp:%s/%s" % (
            params["mcp_server_name"],
            params.get("mcp_tool_name") or "?",
        )
    if params.get("skill_name"):
        return "skill:%s" % params["skill_name"]
    if params.get("subagent_type"):
        return "agent:%s" % params["subagent_type"]
    return tool_name


def _text(v: Any) -> str | None:
    """An OTLP attribute decodes to any JSON type. Normalise it to a non-empty
    string or None: an empty label must stay NULL, otherwise it would show up as
    a project (or a host) of its own next to '(undefined)'."""
    return str(v) if v else None


OUTPUT_STYLE = ":outputStyle:"


def query_origin(query_source: Any) -> str | None:
    """`repl_main_thread:outputStyle:Concise` is the main thread annotated with
    the output style in force. The origin is what precedes that marker.

    The split anchors on the literal, never on the bare `:`: a sub-agent origin
    (`agent:builtin:general-purpose`) has three segments too.

    Anything but a string is None -- an OTLP attribute decodes to any JSON type,
    and raising here would lose the whole batch. The raw value stays in
    `attrs`."""
    if not isinstance(query_source, str):
        return None
    return query_source.split(OUTPUT_STYLE)[0] or None


def output_style(query_source: Any) -> str | None:
    """The style name, or None when none is set -- Claude Code omits the suffix
    on the default style, so no "Default" is invented.

    partition, not split: the style is only ever appended, so a name containing
    a `:` is taken whole to the end of the string."""
    if not isinstance(query_source, str):
        return None
    _, found, style = query_source.partition(OUTPUT_STYLE)
    return style if found else None


def _walk(
    payload: dict[str, Any],
    res_key: str,
    scope_key: str,
    leaf_key: str,
) -> Iterator[tuple[dict[str, JsonValue], dict[str, Any]]]:
    """Every OTLP payload shares the same resourceX -> scopeX -> items shape,
    flattened here so neither ingester repeats the three nesting levels."""
    for r in payload.get(res_key) or []:
        res = kvlist((r.get("resource") or {}).get("attributes"))
        for s in r.get(scope_key) or []:
            for leaf in s.get(leaf_key) or []:
                yield res, leaf


def _temporality(v: Any) -> Any:
    """Depending on the exporter version, temporality arrives as an OTLP integer
    or as a text label. Any other value passes through untouched: a missing
    temporality must stay NULL in the database, not become cumulative."""
    if isinstance(v, str):
        return 1 if "DELTA" in v.upper() else 2
    return v


def _insert_rows(
    table: str,
    rows: list[dict[str, Any]],
    blobs: list[bytes | None] | None = None,
) -> None:
    """Writes rows named by column into `table`, in one batch.

    The keys of the first row are the column list, so a value cannot drift into
    a neighbouring column, and an unknown key fails the batch rather than writing
    part of it. Table and keys are spelled in the calling code, never taken from
    a payload: both reach the SQL as identifiers.

    Args:
        table: Name of the table to insert into.
        rows: One mapping per row, keyed by column name, all sharing the keys
          of the first. Must not be empty.
        blobs: The raw attribute blob of each row, in order, `None` where there
          is none. When given, each non-None blob is written to `event_attrs`
          keyed by the id its row got, in the same transaction. The ids are the
          contiguous run ending at `last_insert_rowid()`, since one batch holds
          the write lock and nothing interleaves."""
    columns = list(rows[0])
    with store.write() as db:
        db.executemany(
            "INSERT INTO %s (%s) VALUES (%s)"
            % (table, ",".join(columns), ",".join("?" * len(columns))),
            [tuple(_scalar(row[col]) for col in columns) for row in rows],
        )
        if blobs is not None:
            last = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            first = last - len(rows) + 1
            db.executemany(
                "INSERT INTO event_attrs (event_id, attrs) VALUES (?,?)",
                [(first + i, b) for i, b in enumerate(blobs) if b is not None],
            )


def _metric_row(
    attrs: dict[str, Any],
    name: str,
    value: float,
    temporality: Any,
    data_point: dict[str, Any],
) -> dict[str, Any]:
    return {
        "ts": nano_to_s(data_point.get("timeUnixNano")),
        "name": name,
        "value": value,
        "temporality": temporality,
        "session_id": attrs.get("session.id"),
        "host": _text(attrs.get("host")),
        "project": _text(attrs.get("project")),
        "model": attrs.get("model"),
        "query_source": attrs.get("query_source"),
        "attr_type": attrs.get("type"),
        "tool_name": attrs.get("tool_name"),
        "skill_name": attrs.get("skill.name"),
        "mcp_server": attrs.get("mcp_server.name"),
        "start_type": attrs.get("start_type"),
        "decision": attrs.get("decision"),
        "attrs": None,
    }


def ingest_metrics(payload: dict[str, Any]) -> tuple[int, int]:
    out: list[dict[str, Any]] = []
    skipped = 0
    for res, m in _walk(payload, "resourceMetrics", "scopeMetrics", "metrics"):
        name, body = m.get("name"), (m.get("sum") or m.get("gauge"))
        if not name or not body:
            skipped += 1
            continue
        temp = _temporality(body.get("aggregationTemporality"))
        for dp in body.get("dataPoints") or []:
            a = dict(res)
            a.update(kvlist(dp.get("attributes")))
            fv = store.as_float(dp.get("asDouble", dp.get("asInt")))
            if fv is None:
                skipped += 1
                continue
            out.append(_metric_row(a, name, fv, temp, dp))
    if out:
        _insert_rows("metric_points", out)
    log_ingest("metrics", len(out), skipped)
    return len(out), skipped


def _event_row(attrs: dict[str, Any], log_record: dict[str, Any]) -> dict[str, Any]:
    name = (
        attrs.get("event.name")
        or log_record.get("eventName")
        or anyvalue(log_record.get("body"))
        or "unknown"
    )
    name = name.replace("claude_code.", "") if isinstance(name, str) else "unknown"
    p = parse_params(attrs)
    # Claude Code fills `tool_input` or `tool_parameters`: Read, Write and Edit
    # carry their target under the first alone.
    ti = store.tool_input(attrs) or {}
    # Prompt content: present only if OTEL_LOG_USER_PROMPTS=1
    ptxt = attrs.get("prompt") or attrs.get("prompt_text") or attrs.get("user_prompt")
    # Keys vary by Claude Code version, so take the first that exists.
    adesc = (
        p.get("description")
        or p.get("prompt")
        or p.get("task")
        or p.get("instructions")
    )
    return {
        "ts": nano_to_s(
            log_record.get("timeUnixNano") or log_record.get("observedTimeUnixNano")
        ),
        "name": name,
        "session_id": attrs.get("session.id"),
        "host": _text(attrs.get("host")),
        "project": _text(attrs.get("project")),
        "prompt_id": attrs.get("prompt.id"),
        "tool_name": attrs.get("tool_name"),
        "label": make_label(attrs.get("tool_name"), p),
        "success": (
            str(attrs.get("success")).lower()
            if attrs.get("success") is not None
            else None
        ),
        "duration_ms": store.as_float(attrs.get("duration_ms")),
        "result_bytes": store.as_int(attrs.get("tool_result_size_bytes")),
        "input_bytes": store.as_int(attrs.get("tool_input_size_bytes")),
        "error_type": attrs.get("error_type"),
        "bash_cmd": p.get("full_command") or p.get("bash_command"),
        "file_path": ti.get("file_path"),
        "skill_name": p.get("skill_name") or attrs.get("skill.name"),
        "mcp_server": p.get("mcp_server_name") or attrs.get("mcp_server.name"),
        "decision": attrs.get("decision"),
        "dec_source": attrs.get("source") or attrs.get("decision_source"),
        "model": attrs.get("model"),
        "query_source": attrs.get("query_source"),
        "query_origin": query_origin(attrs.get("query_source")),
        "output_style": output_style(attrs.get("query_source")),
        "cost_usd": store.as_float(attrs.get("cost_usd")),
        "in_tokens": store.as_int(attrs.get("input_tokens")),
        "out_tokens": store.as_int(attrs.get("output_tokens")),
        "cache_read": store.as_int(attrs.get("cache_read_tokens")),
        "cache_create": store.as_int(attrs.get("cache_creation_tokens")),
        "trigger_kind": attrs.get("trigger"),
        "pre_tokens": store.as_int(attrs.get("pre_tokens")),
        "post_tokens": store.as_int(attrs.get("post_tokens")),
        "prompt_text": ptxt,
        "agent_type": p.get("subagent_type") or attrs.get("agent_type"),
        "agent_desc": adesc,
        # subagent_completed reports its own totals, read by subagents_stats.
        "agent_tokens": store.as_int(attrs.get("total_tokens")),
        "agent_tools": store.as_int(attrs.get("total_tool_uses")),
        # Hook fire (hook_execution_complete) and registration (hook_registered):
        # the overhead the Hooks page and the timeline group and rank.
        "hook_name": attrs.get("hook_name"),
        "hook_event": attrs.get("hook_event"),
        "hook_duration_ms": store.as_float(attrs.get("total_duration_ms")),
        "hook_err": store.as_int(attrs.get("num_non_blocking_error")),
        "hook_block": store.as_int(attrs.get("num_blocking")),
        "num_hooks": store.as_int(attrs.get("num_hooks")),
        "hook_source": attrs.get("hook_source"),
        "hook_type": attrs.get("hook_type"),
        "hook_matcher": attrs.get("hook_matcher"),
        # Provider incidents (api_error, api_retries_exhausted): what the API
        # refused or gave up on, folded by provider_errors.
        "status_code": store.as_int(attrs.get("status_code")),
        "error_text": attrs.get("error"),
        "total_attempts": store.as_int(attrs.get("total_attempts")),
        "attempt": store.as_int(attrs.get("attempt")),
        "retry_duration_ms": store.as_float(attrs.get("total_retry_duration_ms")),
        # api_request reasoning effort, one of the session chips.
        "effort": attrs.get("effort"),
        # Timeline detail read out of the fire, the mode switch, the MCP status
        # change and the mention it summarises. `response` is stored whole and
        # clipped at the read.
        "response": attrs.get("response"),
        "response_length": store.as_int(attrs.get("response_length")),
        "from_mode": attrs.get("from_mode"),
        "to_mode": attrs.get("to_mode"),
        "mcp_status": attrs.get("status"),
        "server_name": attrs.get("server_name"),
        "transport_type": attrs.get("transport_type"),
        "error_name": attrs.get("error_name"),
        "mention_type": attrs.get("mention_type"),
        # Ride on nearly every record: the terminal and CLI version chips.
        "terminal_type": attrs.get("terminal.type"),
        "service_version": attrs.get("service.version"),
        "params": json.dumps(p, ensure_ascii=False) if p else None,
        "attrs": _raw_attrs(name, attrs),
    }


def _raw_attrs(name: str, attrs: dict[str, Any]) -> bytes | None:
    """The attribute blob the event inspector shows, zlib-compressed (#181) --
    store.attrs_json is the one decoder that inflates it. None for the one event
    nothing reads: every attribute of `hook_execution_start` is on the
    `hook_execution_complete` of the same fire, which api_session shows instead.
    Metric points carry no blob at all -- no route opens one by id."""
    if name == "hook_execution_start":
        return None
    return zlib.compress(json.dumps(attrs, ensure_ascii=False).encode(), 6)


def ingest_logs(payload: dict[str, Any]) -> tuple[int, int]:
    """No record is ever rejected, unlike ingest_metrics: one whose event name
    cannot be found is stored as "unknown" with its raw attributes, which stays
    recoverable if the name moves again between Claude Code versions. `skipped`
    is therefore always 0 for logs."""
    out: list[dict[str, Any]] = []
    for res, lr in _walk(payload, "resourceLogs", "scopeLogs", "logRecords"):
        a = dict(res)
        a.update(kvlist(lr.get("attributes")))
        out.append(_event_row(a, lr))
    if out:
        # The blob leaves the events row for its sibling: events keeps the
        # columns every aggregate scans, event_attrs the bytes only the
        # inspector opens (#180).
        blobs = [row.pop("attrs") for row in out]
        _insert_rows("events", out, blobs)
    log_ingest("logs", len(out), 0)
    return len(out), 0


def log_ingest(
    kind: str,
    accepted: int,
    skipped: int,
    note: str | None = None,
) -> None:
    with store.write() as db:
        db.execute(
            "INSERT INTO ingest_log (ts,kind,accepted,skipped,note) VALUES (?,?,?,?,?)",
            (
                int(datetime.now(timezone.utc).timestamp()),
                kind,
                accepted,
                skipped,
                note,
            ),
        )


def inflate(raw: bytes, wbits: int) -> bytes:
    """Bounded decompression: beyond MAX_DECOMPRESSED we stop rather than let a
    gzip bomb saturate memory. wbits=31 for gzip, 15 for zlib."""
    d = zlib.decompressobj(wbits)
    out = d.decompress(raw, MAX_DECOMPRESSED)
    if d.unconsumed_tail:
        raise ValueError("decompressed stream too large")
    return out + d.flush()


class BodyTooLarge(Exception):
    """Raised past MAX_BODY while reading a chunked body, so the handler can
    answer 413 like it does for an oversized Content-Length instead of folding
    the case into the generic 400."""


def read_chunked(rfile: io.BufferedIOBase, limit: int) -> bytes:
    """Reassemble a chunked body. BaseHTTPRequestHandler only honours
    Content-Length, so without this an exporter that streams its body -- which
    HTTP/1.1 allows -- would read as an empty one."""
    out = bytearray()
    while True:
        # Bounded: an endless size line is a denial of service on its own.
        line = rfile.readline(CHUNK_LINE_MAX)
        if not line.endswith(b"\n"):
            raise ValueError("chunk size line too long or truncated")
        # Everything past the ";" is a chunk extension, which is never read.
        size = int(line.split(b";", 1)[0].strip(), 16)
        if size == 0:
            break
        if len(out) + size > limit:
            raise BodyTooLarge(len(out) + size)
        chunk = rfile.read(size)
        if len(chunk) != size:
            raise ValueError("chunk shorter than announced")
        out += chunk
        if rfile.read(2) != b"\r\n":
            raise ValueError("chunk not terminated by CRLF")
    # Trailers, if any, run until the blank line that closes the body.
    while True:
        line = rfile.readline(CHUNK_LINE_MAX)
        if not line or not line.strip():
            break
    return bytes(out)


# Matched by suffix, since an exporter may carry a prefixed base URL. Traces
# are journalled and stored nowhere; refusing them would make it retry.
INGESTERS = [
    ("/v1/metrics", ingest_metrics),
    ("/v1/logs", ingest_logs),
    ("/v1/traces", lambda payload: log_ingest("traces", 0, 0, "traces ignored")),
]
