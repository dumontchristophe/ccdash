"""Storage: the SQLite connection, the schema, the read helpers, and the three
decoders both paths need.
"""

import contextlib
import json
import os
import sqlite3
import stat
import sys
import threading
import zlib
from collections.abc import Iterator, Sequence
from typing import Any, overload

# Lowercase: `main` rebinds both.
db_path = ""
verbose = False

# The -wal and -shm siblings carry the same rows, so they carry the same mode.
DIR_MODE = 0o700
FILE_MODE = 0o600

TABLES = """
CREATE TABLE IF NOT EXISTS metric_points (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, name TEXT NOT NULL,
    value REAL NOT NULL, temporality INTEGER, session_id TEXT, host TEXT,
    project TEXT, model TEXT, query_source TEXT, attr_type TEXT, tool_name TEXT,
    skill_name TEXT, mcp_server TEXT, start_type TEXT, decision TEXT, attrs TEXT);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, name TEXT NOT NULL,
    session_id TEXT, host TEXT, project TEXT, prompt_id TEXT, tool_name TEXT,
    label TEXT, success TEXT, duration_ms REAL, result_bytes INTEGER,
    input_bytes INTEGER, error_type TEXT, bash_cmd TEXT, file_path TEXT,
    skill_name TEXT,
    mcp_server TEXT, decision TEXT, dec_source TEXT, model TEXT, query_source TEXT,
    query_origin TEXT, output_style TEXT,
    cost_usd REAL,
    in_tokens INTEGER, out_tokens INTEGER, cache_read INTEGER, cache_create INTEGER,
    trigger_kind TEXT, pre_tokens INTEGER, post_tokens INTEGER, prompt_text TEXT,
    agent_type TEXT, agent_desc TEXT, agent_tokens INTEGER, agent_tools INTEGER,
    hook_name TEXT, hook_event TEXT, hook_duration_ms REAL, hook_err INTEGER,
    hook_block INTEGER, num_hooks INTEGER, hook_source TEXT, hook_type TEXT,
    hook_matcher TEXT, status_code INTEGER, error_text TEXT, total_attempts INTEGER,
    attempt INTEGER, retry_duration_ms REAL, effort TEXT, response TEXT,
    response_length INTEGER, from_mode TEXT, to_mode TEXT, mcp_status TEXT,
    server_name TEXT, transport_type TEXT, error_name TEXT, mention_type TEXT,
    terminal_type TEXT, service_version TEXT,
    params TEXT, attrs TEXT);

-- The raw attribute blob, one row to one event, in a table no aggregate scans:
-- events keeps the columns a query filters or groups on, this keeps the ~53 %
-- of its bytes only the inspector opens, one row at a time (#180).
CREATE TABLE IF NOT EXISTS event_attrs (
    event_id INTEGER PRIMARY KEY, attrs BLOB);

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, kind TEXT,
    accepted INTEGER, skipped INTEGER, note TEXT);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS ix_mp_name ON metric_points(name);
CREATE INDEX IF NOT EXISTS ix_mp_sess ON metric_points(session_id);
CREATE INDEX IF NOT EXISTS ix_mp_ts   ON metric_points(ts);
CREATE INDEX IF NOT EXISTS ix_mp_prj  ON metric_points(project);
CREATE INDEX IF NOT EXISTS ix_ev_name ON events(name);
CREATE INDEX IF NOT EXISTS ix_ev_sess ON events(session_id);
CREATE INDEX IF NOT EXISTS ix_ev_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS ix_ev_lbl  ON events(label);
CREATE INDEX IF NOT EXISTS ix_ev_prj  ON events(project);
-- Equality first, range second: `WHERE name=? AND ts>=?` is every aggregate.
CREATE INDEX IF NOT EXISTS ix_ev_name_ts ON events(name, ts);
CREATE INDEX IF NOT EXISTS ix_mp_name_ts ON metric_points(name, ts);
-- prompt_stats and api_prompt's span carry prompt_id alone, so without this
-- they full-SCAN the whole history to read one turn.
CREATE INDEX IF NOT EXISTS ix_ev_prompt ON events(prompt_id);
-- file_stats groups the Edit/Write results by path. name leads so the
-- stats-less planner prefers it to ix_ev_name_ts; the partial clause keeps it to
-- those two tools, and the file_path order drops the GROUP BY sort.
CREATE INDEX IF NOT EXISTS ix_ev_file ON events(name, file_path)
    WHERE tool_name IN ('Edit', 'Write');
"""

_db_lock = threading.Lock()
# Dereferenced unguarded: reaching it unopened is a bug.
_db: sqlite3.Connection | None = None


def db_init(path: str) -> None:
    """Opens the connection and creates the schema from TABLES.

    The lock spans the whole call, taken directly."""
    global _db
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d)
        ours = True
    except FileExistsError:
        ours = False
    with _db_lock:
        _db = sqlite3.connect(path, check_same_thread=False)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode=WAL")
        _db.execute("PRAGMA synchronous=NORMAL")
        _db.execute("PRAGMA busy_timeout=5000")
        # KiB, not pages: 64 MB is this connection's ceiling.
        _db.execute("PRAGMA cache_size=-65536")
        _db.executescript(TABLES)
        _db.executescript(INDEXES)
        _db.commit()
        # Only a directory this call created: --db can name a checkout.
        if ours:
            _restrict(d, DIR_MODE)
        for suffix in ("", "-wal", "-shm"):
            _restrict(path + suffix, FILE_MODE)


def _restrict(path: str, mode: int) -> None:
    """Clears the bits `mode` does not grant, never adds one. A filesystem
    refusing the chmod warns and the boot carries on."""
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
        if current & ~mode:
            os.chmod(path, current & mode)
    except OSError as e:
        sys.stderr.write("ccdash: cannot restrict %s: %s\n" % (path, e))


def db_close() -> None:
    """Closes the connection, back to the pre-init state. A second call is a
    no-op, so a caller that already closed still runs its teardown."""
    global _db
    with _db_lock:
        if _db is not None:
            _db.close()
            _db = None


@contextlib.contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """The write path's counterpart to query*: takes the lock, yields the
    connection, commits on a clean exit and rolls back otherwise. The only way
    in from outside this module.

    Yields:
        The open sqlite3 connection, for the duration of the block.
    """
    with _db_lock:
        try:
            yield _db  # type: ignore[misc]
            _db.commit()  # type: ignore[union-attr]
        except Exception:
            _db.rollback()  # type: ignore[union-attr]
            raise


def query(sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
    """Every row `sql` returns, as sqlite3.Row objects, under the connection
    lock. The base read the others build on."""
    with _db_lock:
        return _db.execute(sql, args).fetchall()  # type: ignore[union-attr]


def query_row(sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
    """The first row `sql` returns, or None when it returns none."""
    r = query(sql, args)
    return r[0] if r else None


def query_dicts(sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
    """Every row `sql` returns, each as a plain dict keyed by column name --
    ready to go straight into a payload."""
    return [dict(r) for r in query(sql, args)]


def query_value(sql: str, args: Sequence[Any] = ()) -> Any:
    """First column of the first row, 0 when there is none. Read by position:
    the query does not have to name what it returns."""
    r = query_row(sql, args)
    return (r[0] or 0) if r else 0


# Overloaded because max() and += do not take an Optional.
@overload
def as_int(v: Any, default: int) -> int: ...


@overload
def as_int(v: Any, default: None = None) -> int | None: ...


def as_int(v: Any, default: int | None = None) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@overload
def as_float(v: Any, default: float) -> float: ...


@overload
def as_float(v: Any, default: None = None) -> float | None: ...


def as_float(v: Any, default: float | None = None) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def attrs_json(raw: bytes | str | None) -> str:
    """The stored attribute blob, back as JSON text: the one place that knows it
    is compressed. A BLOB is written zlib-compressed (#181) and returns as bytes;
    a pre-compression row returns its text as-is; a NULL returns "{}". The text
    fallback lets both forms coexist until a rebuild reclaims the old rows."""
    if isinstance(raw, (bytes, bytearray)):
        return zlib.decompress(bytes(raw)).decode()
    return raw or "{}"


def tool_input(attrs: dict[str, Any] | bytes | str | None) -> dict[str, Any] | None:
    """The `tool_input` of a tool_result, which arrives as a dict or as JSON
    depending on the Claude Code version. Takes either, so neither end
    re-encodes. None — not `{}` — when unusable: an empty object is an answer."""
    decoded = attrs if isinstance(attrs, dict) else json.loads(attrs_json(attrs))
    ti = decoded.get("tool_input")
    if isinstance(ti, str) and ti.strip().startswith("{"):
        try:
            ti = json.loads(ti)
        except ValueError:
            ti = {}
    return ti if isinstance(ti, dict) else None
