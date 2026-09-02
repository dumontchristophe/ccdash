# Run: python3 -m unittest discover -s tests -v  (from repo root)
import contextlib
import io
import json
import os
import sys
import time
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base import BaseDBTest

from ccdash import ingest
from ccdash.core import store
from ccdash.pages.details import api_event

# The attribute types Claude Code really sends: a tool_result states its numbers
# as strings, an api_request as integers, cost_usd as a double, and only is_async
# and is_built_in as booleans. A test using another form on purpose says so.


def _attr(key, value_dict):
    return {"key": key, "value": value_dict}


def _str(v):
    return {"stringValue": v}


def _int(v):
    return {"intValue": str(v)}


def _bool(v):
    return {"boolValue": v}


def _dbl(v):
    return {"doubleValue": v}


def _kvlist(**pairs):
    return {"kvlistValue": {"values": [_attr(k, _str(v)) for k, v in pairs.items()]}}


def _array(*items):
    return {"arrayValue": {"values": [_str(i) for i in items]}}


def now_ns():
    return str(int(time.time() * 1_000_000_000))


def metric_payload(name, value, attributes=None, temporality=None, as_double=False):
    """Builds a minimal OTLP metrics payload."""
    dp = {
        "timeUnixNano": now_ns(),
        "attributes": attributes or [],
    }
    if as_double:
        dp["asDouble"] = float(value)
    else:
        dp["asInt"] = int(value)

    body = {"dataPoints": [dp]}
    if temporality is not None:
        body["aggregationTemporality"] = temporality

    return {
        "resourceMetrics": [
            {
                "resource": {"attributes": []},
                "scopeMetrics": [{"metrics": [{"name": name, "sum": body}]}],
            }
        ]
    }


def log_payload(event_name, attributes=None, session_id="sess-log-1"):
    """Builds a minimal OTLP logs payload."""
    base_attrs = [
        _attr("event.name", _str("claude_code." + event_name)),
        _attr("session.id", _str(session_id)),
    ]
    if attributes:
        base_attrs.extend(attributes)
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": now_ns(),
                                "attributes": base_attrs,
                            }
                        ]
                    }
                ],
            }
        ]
    }


class TestSchemaIndexes(BaseDBTest):
    """The indexes db_init applies.

    A timing assertion would measure the machine running it, not the schema, so
    what is pinned here is the declaration: without (name, ts) SQLite seeks on
    the name and discards by date row by row, and the cost of one day's window
    grows with the whole history rather than with the window.
    """

    def _plan(self, sql, args=()):
        return " ".join(
            r["detail"] for r in store.query("EXPLAIN QUERY PLAN " + sql, args)
        )

    def test_the_planner_uses_the_composite_for_a_filtered_aggregate(self):
        # The declaration is not the point on its own: SQLite is free to prefer
        # ix_ev_name and silently give back the walk this index exists to avoid.
        plan = self._plan(
            "SELECT COUNT(*) FROM events WHERE name=? AND ts>=?", ("tool_result", 0)
        )
        self.assertIn("ix_ev_name_ts", plan)

    def test_the_planner_seeks_prompt_id_instead_of_scanning(self):
        # prompt_stats and api_prompt's span carry prompt_id as their only
        # constraint: without ix_ev_prompt they fall to a full SCAN that grows
        # with the whole history rather than with the one turn read.
        plan = self._plan(
            "SELECT MIN(ts), MAX(ts) FROM events WHERE prompt_id=?", ("p",)
        )
        self.assertIn("ix_ev_prompt", plan)
        self.assertNotIn("SCAN events", plan)

    def test_the_planner_uses_the_partial_index_for_file_stats(self):
        # The stats-less planner picks a bare events(file_path) over ix_ev_name_ts
        # only when name leads it; the partial clause keeps it to the two tools
        # file_stats counts, and the file_path order drops the GROUP BY sort.
        plan = self._plan(
            "SELECT file_path, COUNT(*) FROM events WHERE name='tool_result' "
            "AND file_path IS NOT NULL AND tool_name IN ('Edit','Write') "
            "GROUP BY file_path",
            (),
        )
        self.assertIn("ix_ev_file", plan)
        self.assertNotIn("TEMP B-TREE FOR GROUP BY", plan)


class TestIngestMetrics(BaseDBTest):
    """Tests for ingest_metrics."""

    def _token_payload(
        self, session_id, model, token_type, value, host="myhost", project="myproject"
    ):
        attrs = [
            _attr("session.id", _str(session_id)),
            _attr("model", _str(model)),
            _attr("type", _str(token_type)),
            _attr("host", _str(host)),
            _attr("project", _str(project)),
        ]
        return metric_payload("claude_code.token.usage", value, attrs)

    def test_row_lands_in_db(self):
        payload = self._token_payload("s1", "claude-opus-4-8", "output", 50)
        ingest.ingest_metrics(payload)
        rows = store.query_dicts("SELECT * FROM metric_points")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["name"], "claude_code.token.usage")
        self.assertEqual(row["value"], 50.0)
        self.assertEqual(row["session_id"], "s1")
        self.assertEqual(row["host"], "myhost")
        self.assertEqual(row["project"], "myproject")
        self.assertEqual(row["attr_type"], "output")

    def test_a_temporality_label_maps_to_its_otlp_code(self):
        """Some exporter versions send the label instead of the integer, and the
        cost views read the code alone."""
        for label, code in (
            ("AGGREGATION_TEMPORALITY_DELTA", 1),
            ("AGGREGATION_TEMPORALITY_CUMULATIVE", 2),
        ):
            with self.subTest(temporality=label):
                with store.write() as db:
                    db.execute("DELETE FROM metric_points")
                payload = {
                    "resourceMetrics": [
                        {
                            "resource": {"attributes": []},
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": "claude_code.cost.usage",
                                            "sum": {
                                                "aggregationTemporality": label,
                                                "dataPoints": [
                                                    {
                                                        "timeUnixNano": now_ns(),
                                                        "asDouble": 0.01,
                                                        "attributes": [
                                                            _attr(
                                                                "session.id", _str("s1")
                                                            )
                                                        ],
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
                ingest.ingest_metrics(payload)
                row = store.query_dicts("SELECT temporality FROM metric_points")[0]
                self.assertEqual(row["temporality"], code)

    def test_no_value_skipped(self):
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": now_ns(),
                                                "attributes": [],
                                                # No asInt and no asDouble -> skipped
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_metrics(payload)
        self.assertEqual(accepted, 0)
        self.assertEqual(skipped, 1)

    def test_no_metric_name_skipped(self):
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    # No "name"
                                    "sum": {"dataPoints": []}
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_metrics(payload)
        self.assertEqual(accepted, 0)
        self.assertEqual(skipped, 1)

    def test_multiple_datapoints(self):
        dp_list = [
            {
                "timeUnixNano": now_ns(),
                "asInt": 10,
                "attributes": [_attr("type", _str("input"))],
            },
            {
                "timeUnixNano": now_ns(),
                "asInt": 20,
                "attributes": [_attr("type", _str("output"))],
            },
        ]
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": [_attr("session.id", _str("s1"))]},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {"dataPoints": dp_list},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_metrics(payload)
        self.assertEqual(accepted, 2)

    def test_ingest_log_written(self):
        payload = self._token_payload("s1", "claude-opus-4-8", "input", 100)
        ingest.ingest_metrics(payload)
        log_rows = store.query_dicts("SELECT * FROM ingest_log WHERE kind='metrics'")
        self.assertEqual(len(log_rows), 1)
        self.assertEqual(log_rows[0]["accepted"], 1)
        self.assertEqual(log_rows[0]["skipped"], 0)

    def test_gauge_metric(self):
        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.session.count",
                                    "gauge": {
                                        "dataPoints": [
                                            {
                                                "timeUnixNano": now_ns(),
                                                "asInt": 1,
                                                "attributes": [
                                                    _attr("session.id", _str("s2"))
                                                ],
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_metrics(payload)
        self.assertEqual(accepted, 1)


class TestIngestLogs(BaseDBTest):
    """Tests for ingest_logs."""

    def test_an_unparseable_timestamp_is_stored_as_now_and_logged(self):
        payload = log_payload("tool_result")
        record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        record["timeUnixNano"] = "yesterday"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            accepted, skipped = ingest.ingest_logs(payload)
        self.assertEqual((accepted, skipped), (1, 0))
        row = store.query_dicts("SELECT ts FROM events")[0]
        self.assertAlmostEqual(row["ts"], int(time.time()), delta=5)
        self.assertIn("yesterday", err.getvalue())

    def test_a_boolean_success_is_normalised_to_a_string(self):
        # Defensive: this export sends "true"/"false" as strings, but the column
        # is compared to 'false' everywhere, so a boolValue from another version
        # has to land in the same shape rather than as 'True'.
        payload = log_payload(
            "tool_result",
            [
                _attr("tool_name", _str("Edit")),
                _attr("success", _bool(True)),
            ],
        )
        ingest.ingest_logs(payload)
        row = store.query_dicts("SELECT success FROM events")[0]
        self.assertEqual(row["success"], "true")


GOLDEN_NS = "1700000000000000000"
GOLDEN_TS = 1700000000

# (label, metric name, value, attributes, the columns it must fill). One case per
# shape Claude Code exports, since no single record carries every column. Every
# other column must stay NULL, which catches a permutation of two typed fields.
METRIC_CASES = [
    (
        "token.usage",
        "claude_code.token.usage",
        1234.5,
        [
            _attr("model", _str("claude-opus-4-8[1m]")),
            _attr("query_source", _str("main")),
            _attr("type", _str("cacheRead")),
            _attr("effort", _str("high")),
            _attr("skill.name", _str("skill-g1")),
            _attr("mcp_server.name", _str("mcp-g1")),
            _attr("project", _str("proj-g1")),
        ],
        {
            "project": "proj-g1",
            "model": "claude-opus-4-8[1m]",
            "query_source": "main",
            "attr_type": "cacheRead",
            "skill_name": "skill-g1",
            "mcp_server": "mcp-g1",
        },
    ),
    (
        "code_edit_tool.decision",
        "claude_code.code_edit_tool.decision",
        1.0,
        [
            _attr("decision", _str("accept")),
            _attr("source", _str("config")),
            _attr("tool_name", _str("Write")),
            _attr("language", _str("Markdown")),
        ],
        {
            "decision": "accept",
            "tool_name": "Write",
        },
    ),
    (
        "session.count",
        "claude_code.session.count",
        1.0,
        [
            _attr("start_type", _str("fresh")),
        ],
        {
            "start_type": "fresh",
        },
    ),
]

# (label, event name, attributes, the columns it must fill)
EVENT_CASES = [
    (
        "tool_result (Bash, failed)",
        "tool_result",
        [
            _attr("project", _str("proj-g2")),
            _attr("prompt.id", _str("prompt-g2")),
            _attr("tool_name", _str("Bash")),
            _attr("tool_use_id", _str("toolu_g2")),
            _attr("success", _str("false")),
            _attr("duration_ms", _str("49")),
            _attr("error_type", _str("ShellError")),
            _attr("error", _str("Shell command failed")),
            _attr("tool_input_size_bytes", _str("229")),
            _attr("tool_result_size_bytes", _str("50")),
            # decision_type never feeds the `decision` column, which the
            # tool_decision event writes: the two must not collide.
            _attr("decision_type", _str("accept")),
            _attr("decision_source", _str("config")),
            _attr(
                "tool_parameters",
                _str(
                    json.dumps(
                        {
                            "bash_command": "grep",
                            "full_command": "grep -rn ccdash .",
                            "description": "the description",
                        }
                    )
                ),
            ),
        ],
        {
            "project": "proj-g2",
            "prompt_id": "prompt-g2",
            "tool_name": "Bash",
            "label": "Bash",
            "success": "false",
            "duration_ms": 49.0,
            "error_type": "ShellError",
            "error_text": "Shell command failed",
            "input_bytes": 229,
            "result_bytes": 50,
            "dec_source": "config",
            "bash_cmd": "grep -rn ccdash .",
            "agent_desc": "the description",
            "params": {
                "bash_command": "grep",
                "full_command": "grep -rn ccdash .",
                "description": "the description",
            },
        },
    ),
    (
        "tool_result (mcp_tool)",
        "tool_result",
        [
            _attr("tool_name", _str("mcp_tool")),
            _attr("success", _str("true")),
            _attr(
                "tool_parameters",
                _str(
                    json.dumps(
                        {
                            "mcp_server_name": "srv",
                            "mcp_tool_name": "fetch",
                        }
                    )
                ),
            ),
        ],
        {
            "tool_name": "mcp_tool",
            # the server named in tool_parameters wins over the generic tool name
            "label": "mcp:srv/fetch",
            "success": "true",
            "mcp_server": "srv",
            "params": {"mcp_server_name": "srv", "mcp_tool_name": "fetch"},
        },
    ),
    # The file tools carry their target under `tool_input` and never under
    # `tool_parameters`, so `params` stays NULL here while `file_path` fills.
    (
        "tool_result (Read)",
        "tool_result",
        [
            _attr("tool_name", _str("Read")),
            _attr("success", _str("true")),
            _attr(
                "tool_input",
                _str(
                    json.dumps(
                        {
                            "file_path": "/home/u/proj/app/ccdash.py",
                            "offset": 655,
                            "limit": 1941,
                        }
                    )
                ),
            ),
        ],
        {
            "tool_name": "Read",
            "label": "Read",
            "success": "true",
            "file_path": "/home/u/proj/app/ccdash.py",
        },
    ),
    (
        "tool_decision",
        "tool_decision",
        [
            _attr("decision", _str("reject")),
            _attr("source", _str("user_reject")),
            _attr("tool_name", _str("Bash")),
            _attr("tool_use_id", _str("toolu_g3")),
        ],
        {
            "decision": "reject",
            "dec_source": "user_reject",
            "tool_name": "Bash",
            "label": "Bash",
        },
    ),
    (
        "api_request",
        "api_request",
        [
            _attr("prompt.id", _str("prompt-g2")),
            _attr("model", _str("claude-opus-4-8")),
            _attr("input_tokens", _int(4377)),
            _attr("output_tokens", _int(260)),
            _attr("cache_read_tokens", _int(15394)),
            _attr("cache_creation_tokens", _int(4880)),
            _attr("cost_usd", _dbl(0.066582)),
            _attr("duration_ms", _int(9854)),
            _attr("query_source", _str("repl_main_thread")),
            _attr("skill.name", _str("skill-g2")),
        ],
        {
            "prompt_id": "prompt-g2",
            "model": "claude-opus-4-8",
            "in_tokens": 4377,
            "out_tokens": 260,
            "cache_read": 15394,
            "cache_create": 4880,
            "cost_usd": 0.066582,
            "duration_ms": 9854.0,
            "query_source": "repl_main_thread",
            "query_origin": "repl_main_thread",
            "skill_name": "skill-g2",
        },
    ),
    # The same shape under a named output style: query_source keeps the wire
    # value verbatim, and the two derived columns are what the reads match on.
    (
        "api_request (output style)",
        "api_request",
        [
            _attr("model", _str("claude-opus-4-8")),
            _attr("input_tokens", _int(2)),
            _attr("query_source", _str("repl_main_thread:outputStyle:Concise")),
        ],
        {
            "model": "claude-opus-4-8",
            "in_tokens": 2,
            "query_source": "repl_main_thread:outputStyle:Concise",
            "query_origin": "repl_main_thread",
            "output_style": "Concise",
        },
    ),
    (
        "compaction",
        "compaction",
        [
            _attr("trigger", _str("manual")),
            _attr("success", _str("true")),
            _attr("duration_ms", _str("123193")),
            _attr("pre_tokens", _str("83487")),
            _attr("post_tokens", _str("14150")),
        ],
        {
            "trigger_kind": "manual",
            "success": "true",
            "duration_ms": 123193.0,
            "pre_tokens": 83487,
            "post_tokens": 14150,
        },
    ),
    (
        "user_prompt",
        "user_prompt",
        [
            _attr("prompt_length", _str("5")),
            _attr("prompt", _str("/exit")),
            _attr("command_name", _str("exit")),
        ],
        {
            "prompt_text": "/exit",
        },
    ),
    (
        "subagent_completed",
        "subagent_completed",
        [
            _attr("agent_type", _str("general-purpose")),
            _attr("agent.source", _str("built-in")),
            _attr("is_built_in", _bool(True)),
            _attr("is_async", _bool(True)),
            _attr("model", _str("claude-sonnet-4-6")),
            _attr("total_tokens", _int(56382)),
            _attr("total_tool_uses", _int(20)),
            _attr("duration_ms", _int(257975)),
        ],
        {
            "agent_type": "general-purpose",
            "model": "claude-sonnet-4-6",
            "duration_ms": 257975.0,
            "agent_tokens": 56382,
            "agent_tools": 20,
        },
    ),
    (
        "hook_execution_complete",
        "hook_execution_complete",
        [
            _attr("hook_name", _str("PostToolUse:Bash")),
            _attr("hook_event", _str("PostToolUse")),
            _attr("num_hooks", _int(3)),
            _attr("total_duration_ms", _str("52")),
            _attr("num_non_blocking_error", _str("1")),
            _attr("num_blocking", _str("0")),
        ],
        {
            "hook_name": "PostToolUse:Bash",
            "hook_event": "PostToolUse",
            "num_hooks": 3,
            "hook_duration_ms": 52.0,
            "hook_err": 1,
            "hook_block": 0,
        },
    ),
    (
        "hook_registered",
        "hook_registered",
        [
            _attr("hook_event", _str("PreToolUse")),
            _attr("hook_source", _str("userSettings")),
            _attr("hook_type", _str("command")),
            _attr("hook_matcher", _str("Bash")),
        ],
        {
            "hook_event": "PreToolUse",
            "hook_source": "userSettings",
            "hook_type": "command",
            "hook_matcher": "Bash",
        },
    ),
    (
        "api_error",
        "api_error",
        [
            _attr("status_code", _int(529)),
            _attr("error", _str("Overloaded")),
            _attr("attempt", _int(3)),
        ],
        {
            "status_code": 529,
            "error_text": "Overloaded",
            "attempt": 3,
        },
    ),
    (
        "api_retries_exhausted",
        "api_retries_exhausted",
        [
            _attr("total_attempts", _str("11")),
            _attr("total_retry_duration_ms", _str("195164")),
        ],
        {
            "total_attempts": 11,
            "retry_duration_ms": 195164.0,
        },
    ),
    # The chips a session is read under, plus the response a timeline row clips.
    (
        "api_request (chips and response)",
        "api_request",
        [
            _attr("effort", _str("high")),
            _attr("response", _str("hello world")),
            _attr("response_length", _int(11)),
            _attr("terminal.type", _str("ghostty")),
            _attr("service.version", _str("2.1.0")),
        ],
        {
            "effort": "high",
            "response": "hello world",
            "response_length": 11,
            "terminal_type": "ghostty",
            "service_version": "2.1.0",
        },
    ),
    # Mode switch, MCP status change and mention: the rest of the timeline detail.
    (
        "mode / mcp / mention",
        "mcp_server_status",
        [
            _attr("from_mode", _str("plan")),
            _attr("to_mode", _str("default")),
            _attr("status", _str("connected")),
            _attr("server_name", _str("filesystem")),
            _attr("transport_type", _str("stdio")),
            _attr("error_name", _str("Timeout")),
            _attr("mention_type", _str("file")),
        ],
        {
            "from_mode": "plan",
            "to_mode": "default",
            "mcp_status": "connected",
            "server_name": "filesystem",
            "transport_type": "stdio",
            "error_name": "Timeout",
            "mention_type": "file",
        },
    ),
]


class TestWhatIngestionDropsAndWhatItKeeps(BaseDBTest):
    """The storage guarantee `docs/backend.md` states, on stored values.

    The six keys of DROP_ATTRS go; everything else is stored as sent -- prompt
    text, shell command and absolute path included.
    """

    PROMPT = "/home/someone/secret -- drop table"
    CMD = "rm -rf /home/someone/secret && echo done"
    PATH = "/home/someone/secret/notes.md"

    def test_the_ingestion_drops_the_named_attributes_and_stores_the_rest_as_sent(self):
        payload = log_payload(
            "user_prompt",
            [
                _attr("user.email", _str("secret@example.com")),
                _attr("user.id", _str("uid")),
                _attr("user.account_uuid", _str("uuid")),
                _attr("user.account_id", _str("acct")),
                _attr("organization.id", _str("org")),
                _attr("user.groups", _str("g1,g2")),
                _attr("prompt", _str(self.PROMPT)),
                _attr("tool_parameters", _str(json.dumps({"full_command": self.CMD}))),
                _attr("tool_input", _str(json.dumps({"file_path": self.PATH}))),
            ],
        )
        ingest.ingest_logs(payload)
        row = store.query_dicts(
            "SELECT prompt_text, bash_cmd, file_path, event_attrs.attrs FROM events "
            "LEFT JOIN event_attrs ON event_attrs.event_id = events.id"
        )[0]

        stored = json.loads(store.attrs_json(row["attrs"]))
        # Named here rather than looped over `ingest.DROP_ATTRS`: a key removed
        # from the constant would make that loop check one key fewer and stay
        # green while the attribute reached storage.
        for k in (
            "user.email",
            "user.id",
            "user.account_uuid",
            "user.account_id",
            "organization.id",
            "user.groups",
        ):
            self.assertNotIn(k, stored)
        self.assertEqual(row["prompt_text"], self.PROMPT)
        self.assertEqual(row["bash_cmd"], self.CMD)
        self.assertEqual(row["file_path"], self.PATH)


class TestWhichEventsKeepTheirBlob(BaseDBTest):
    def test_hook_start_stores_no_blob_and_hook_complete_keeps_it(self):
        attrs = [
            _attr("hook_name", _str("lint")),
            _attr("hook_event", _str("PostToolUse")),
        ]
        ingest.ingest_logs(log_payload("hook_execution_start", attrs))
        ingest.ingest_logs(log_payload("hook_execution_complete", attrs))
        rows = store.query_dicts(
            "SELECT events.name, event_attrs.attrs FROM events "
            "LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
            "ORDER BY events.id"
        )
        self.assertEqual(
            [r["name"] for r in rows],
            ["hook_execution_start", "hook_execution_complete"],
        )
        # The start stored no blob, so it has no event_attrs row at all.
        self.assertIsNone(rows[0]["attrs"])
        self.assertEqual(
            json.loads(store.attrs_json(rows[1]["attrs"]))["hook_name"], "lint"
        )


class TestTheBlobIsStoredCompressed(BaseDBTest):
    """The raw attribute blob is zlib-compressed on the way in (#181), and
    store.attrs_json is the one decoder that inflates it -- tolerating a
    pre-compression text row so both forms coexist until a rebuild."""

    def test_the_written_blob_is_a_compressed_binary_round_trip(self):
        ingest.ingest_logs(log_payload("user_prompt", [_attr("prompt", _str("hello"))]))
        blob = store.query_value("SELECT attrs FROM event_attrs")
        self.assertIsInstance(blob, bytes)
        self.assertEqual(json.loads(zlib.decompress(blob))["prompt"], "hello")

    def test_attrs_json_inflates_what_the_ingester_compressed(self):
        text = json.dumps({"prompt": "hi"})
        self.assertEqual(store.attrs_json(zlib.compress(text.encode(), 6)), text)

    def test_attrs_json_passes_a_pre_compression_text_row_through(self):
        self.assertEqual(store.attrs_json('{"prompt": "hi"}'), '{"prompt": "hi"}')

    def test_attrs_json_reads_a_missing_blob_as_the_empty_object(self):
        self.assertEqual(store.attrs_json(None), "{}")

    def test_a_legacy_uncompressed_row_is_still_decoded(self):
        # A row written before #181 holds JSON text, not a compressed blob. The
        # inspector must keep reading it until a rebuild reclaims it.
        ingest.ingest_logs(log_payload("tool_result"))
        event_id = store.query_value("SELECT id FROM events")
        with store.write() as db:
            db.execute(
                "UPDATE event_attrs SET attrs=? WHERE event_id=?",
                (json.dumps({"tool_input": {"file_path": "/w/a.py"}}), event_id),
            )
        self.assertEqual(api_event(event_id)["tool_input"]["file_path"], "/w/a.py")


class TestRowMappingGolden(BaseDBTest):
    """Net against a value reaching the wrong column.

    Both ingesters name their columns, so no permutation shifts a whole row.
    What stays possible is one value read from the wrong attribute, and swapping
    two keys of the same type (two TEXT, two INTEGER) breaks no targeted test.
    So each case below compares the *whole* row, the columns it does not name
    included -- they have to be NULL.
    """

    def _golden(self, table, expected):
        """The whole row against `expected`, every unnamed column NULL."""
        row = dict(store.query_dicts("SELECT * FROM " + table)[0])
        event_id = row.pop("id")
        attrs = row.pop("attrs")
        # An event's blob moved to the sibling table (#180); metric_points never
        # had one, so its NULL column is the answer.
        if table == "events":
            sibling = store.query_row(
                "SELECT attrs FROM event_attrs WHERE event_id=?", (event_id,)
            )
            attrs = sibling["attrs"] if sibling else None
        attrs = json.loads(store.attrs_json(attrs)) if attrs is not None else None
        if "params" in row and row["params"]:
            row["params"] = json.loads(row["params"])
        self.assertEqual(row, dict({k: None for k in row}, **expected))
        return attrs

    def test_metric_rows_per_shape(self):
        for label, name, value, attributes, expected in METRIC_CASES:
            with self.subTest(shape=label):
                with store.write() as db:
                    db.execute("DELETE FROM metric_points")
                payload = {
                    "resourceMetrics": [
                        {
                            "resource": {
                                "attributes": [_attr("host", _str("res-host"))]
                            },
                            "scopeMetrics": [
                                {
                                    "metrics": [
                                        {
                                            "name": name,
                                            "sum": {
                                                "aggregationTemporality": 1,
                                                "dataPoints": [
                                                    {
                                                        "timeUnixNano": GOLDEN_NS,
                                                        "asDouble": value,
                                                        "attributes": [
                                                            _attr(
                                                                "session.id",
                                                                _str("sess-g1"),
                                                            )
                                                        ]
                                                        + attributes,
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
                self.assertEqual(ingest.ingest_metrics(payload), (1, 0))
                attrs = self._golden(
                    "metric_points",
                    dict(
                        expected,
                        ts=GOLDEN_TS,
                        name=name,
                        value=value,
                        temporality=1,
                        session_id="sess-g1",
                        host="res-host",
                    ),
                )
                # Resource attributes are merged underneath the dataPoint's own,
                # and the merged result reaches the columns only: no blob.
                self.assertIsNone(attrs)

    def test_event_rows_per_shape(self):
        for label, name, attributes, expected in EVENT_CASES:
            with self.subTest(shape=label):
                with store.write() as db:
                    db.execute("DELETE FROM events")
                    # The sibling holds no FK, so a reused rowid would collide.
                    db.execute("DELETE FROM event_attrs")
                payload = {
                    "resourceLogs": [
                        {
                            "resource": {
                                "attributes": [_attr("host", _str("res-host"))]
                            },
                            "scopeLogs": [
                                {
                                    "logRecords": [
                                        {
                                            "timeUnixNano": GOLDEN_NS,
                                            "attributes": [
                                                _attr(
                                                    "event.name",
                                                    _str("claude_code." + name),
                                                ),
                                                _attr("session.id", _str("sess-g2")),
                                            ]
                                            + attributes,
                                        }
                                    ]
                                }
                            ],
                        }
                    ]
                }
                self.assertEqual(ingest.ingest_logs(payload), (1, 0))
                attrs = self._golden(
                    "events",
                    dict(
                        expected,
                        ts=GOLDEN_TS,
                        name=name,
                        session_id="sess-g2",
                        host="res-host",
                    ),
                )
                self.assertEqual(attrs["host"], "res-host")

    def test_row_builders_name_the_columns_of_their_table(self):
        """A builder names every column of its table, and only those.

        A row is a mapping, so no value can land in the column next to its own;
        what has to hold is the set of names. Compared unordered on purpose: a
        column added by a migration sits at the end of the physical table and in
        the middle of TABLES, and that difference is not a defect."""
        for table, row in (
            ("metric_points", ingest._metric_row({}, "m", 1.0, 1, {})),
            ("events", ingest._event_row({}, {})),
        ):
            with self.subTest(table=table):
                columns = {
                    r["name"] for r in store.query("PRAGMA table_info(%s)" % table)
                }
                self.assertEqual(set(row), columns - {"id"})

    def test_every_column_is_reached_by_a_case(self):
        """The split above is only a net as long as every column is still filled
        by one shape. A column nobody fills is a column no permutation moves."""
        for table, cases, always in (
            (
                "metric_points",
                [c[-1] for c in METRIC_CASES],
                {"ts", "name", "value", "temporality", "session_id", "host"},
            ),
            (
                "events",
                [c[-1] for c in EVENT_CASES],
                {"ts", "name", "session_id", "host"},
            ),
        ):
            with self.subTest(table=table):
                columns = {
                    r["name"] for r in store.query("PRAGMA table_info(%s)" % table)
                }
                reached = always.union(*(set(c) for c in cases))
                # `attrs` holds the raw blob and is asserted on its own.
                self.assertEqual(columns - reached - {"id", "attrs"}, set())

    def test_event_row_all_optionals_absent(self):
        """Symmetric to the previous one: the tuple must stay NULL everywhere
        rather than shift the present values into the wrong columns."""
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {"logRecords": [{"timeUnixNano": "1700000000000000000"}]}
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_logs(payload)
        self.assertEqual((accepted, skipped), (1, 0))

        row = dict(store.query_dicts("SELECT * FROM events")[0])
        self.assertEqual(row["ts"], 1700000000)
        self.assertEqual(row["name"], "unknown")
        self.assertIsNone(row["params"])
        # The blob is stored, but in the sibling table; events.attrs stays NULL.
        self.assertIsNone(row["attrs"])
        self.assertEqual(
            store.attrs_json(store.query_value("SELECT attrs FROM event_attrs")),
            "{}",
        )
        nulls = [
            k
            for k, v in row.items()
            if k not in ("id", "ts", "name", "attrs") and v is not None
        ]
        self.assertEqual(nulls, [])

    def test_no_log_record_is_ever_dropped(self):
        """Explicit contract: ingest_logs has no rejection path.

        The event name is looked up in three places because it has already
        moved between two versions of Claude Code. Rejecting nameless records,
        by symmetry with ingest_metrics, would destroy all telemetry the day
        that key moves a fourth time. This test pins the opposite choice: keep
        everything, `skipped` is 0 by construction.
        """
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                # Name in the attribute: the normal case.
                                {
                                    "timeUnixNano": "1700000000000000000",
                                    "attributes": [
                                        {
                                            "key": "event.name",
                                            "value": {
                                                "stringValue": "claude_code.user_prompt"
                                            },
                                        }
                                    ],
                                },
                                # Name only in the record body.
                                {
                                    "timeUnixNano": "1700000001000000000",
                                    "body": {"stringValue": "claude_code.api_request"},
                                },
                                # No name anywhere: kept, not dropped.
                                {"timeUnixNano": "1700000002000000000"},
                            ]
                        }
                    ],
                }
            ]
        }
        accepted, skipped = ingest.ingest_logs(payload)
        self.assertEqual((accepted, skipped), (3, 0))

        names = [
            r["name"] for r in store.query_dicts("SELECT name FROM events ORDER BY ts")
        ]
        self.assertEqual(names, ["user_prompt", "api_request", "unknown"])

        # The ingestion journal must reflect the three accepted and zero rejected.
        log_row = store.query_dicts("SELECT * FROM ingest_log WHERE kind='logs'")[-1]
        self.assertEqual((log_row["accepted"], log_row["skipped"]), (3, 0))


class TestNonScalarAttributes(BaseDBTest):
    """One structured attribute must not take down the whole batch.

    `anyvalue` decodes a kvlistValue into a dict and an arrayValue into a list.
    sqlite refuses to bind those types, and since both ingesters insert via
    `executemany`, a single unexpected attribute made all hundred records of the
    batch fail. The contract stated by `ingest_logs`'s docstring is the opposite:
    keep what we can rather than destroy telemetry.
    """

    def test_a_structured_attribute_does_not_lose_the_log_batch(self):
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": "1700000000000000000",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr("model", _str("claude-opus-4-8")),
                                    ],
                                },
                                {
                                    "timeUnixNano": "1700000000000000001",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr("model", _kvlist(nested="oops")),
                                        _attr("decision", _array("a", "b")),
                                    ],
                                },
                                {
                                    "timeUnixNano": "1700000000000000002",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr(
                                            "model", _str("claude-haiku-4-5-20251001")
                                        ),
                                    ],
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        self.assertEqual(ingest.ingest_logs(payload), (3, 0))

        rows = store.query_dicts(
            "SELECT model, decision, event_attrs.attrs FROM events "
            "LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
            "ORDER BY events.id"
        )
        self.assertEqual(len(rows), 3)
        # Both healthy neighbours are intact: that is the whole point of the batch.
        self.assertEqual(rows[0]["model"], "claude-opus-4-8")
        self.assertEqual(rows[2]["model"], "claude-haiku-4-5-20251001")
        # The column is blanked, but the raw value stays readable in `attrs`.
        self.assertIsNone(rows[1]["model"])
        self.assertIsNone(rows[1]["decision"])
        attrs = json.loads(store.attrs_json(rows[1]["attrs"]))
        self.assertEqual(attrs["model"], {"nested": "oops"})
        self.assertEqual(attrs["decision"], ["a", "b"])

    def test_a_structured_query_source_does_not_lose_the_log_batch(self):
        """`query_source` is the one attribute _event_row parses rather than
        stores: the origin and the style are split off it before _scalar ever
        sees it. A dict there reaches the row builder, one step earlier than the
        blanking above: unhandled it costs the whole batch, not the column."""
        payload = {
            "resourceLogs": [
                {
                    "resource": {"attributes": []},
                    "scopeLogs": [
                        {
                            "logRecords": [
                                {
                                    "timeUnixNano": "1700000000000000000",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr("query_source", _str("repl_main_thread")),
                                    ],
                                },
                                {
                                    "timeUnixNano": "1700000000000000001",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr("query_source", _kvlist(nested="oops")),
                                    ],
                                },
                                {
                                    "timeUnixNano": "1700000000000000002",
                                    "attributes": [
                                        _attr(
                                            "event.name",
                                            _str("claude_code.api_request"),
                                        ),
                                        _attr("query_source", _int(5)),
                                    ],
                                },
                            ]
                        }
                    ],
                }
            ]
        }
        self.assertEqual(ingest.ingest_logs(payload), (3, 0))

        rows = store.query_dicts(
            "SELECT query_source, query_origin, output_style, event_attrs.attrs "
            "FROM events LEFT JOIN event_attrs ON event_attrs.event_id = events.id "
            "ORDER BY events.id"
        )
        self.assertEqual(rows[0]["query_origin"], "repl_main_thread")
        # Neither shape carries the marker, so neither derived column can say
        # anything -- and the raw value stays readable in `attrs`.
        self.assertEqual(
            (rows[1]["query_origin"], rows[1]["output_style"]), (None, None)
        )
        self.assertEqual(
            (rows[2]["query_origin"], rows[2]["output_style"]), (None, None)
        )
        self.assertEqual(
            json.loads(store.attrs_json(rows[1]["attrs"]))["query_source"],
            {"nested": "oops"},
        )

    def test_a_structured_attribute_does_not_lose_the_metric_batch(self):
        def point(ts, attributes):
            return {"timeUnixNano": ts, "asDouble": 1.0, "attributes": attributes}

        payload = {
            "resourceMetrics": [
                {
                    "resource": {"attributes": []},
                    "scopeMetrics": [
                        {
                            "metrics": [
                                {
                                    "name": "claude_code.token.usage",
                                    "sum": {
                                        "aggregationTemporality": 1,
                                        "dataPoints": [
                                            point(
                                                "1700000000000000000",
                                                [_attr("model", _str("ok-1"))],
                                            ),
                                            point(
                                                "1700000000000000001",
                                                [
                                                    _attr("model", _kvlist(n="oops")),
                                                    _attr("type", _array("x")),
                                                ],
                                            ),
                                            point(
                                                "1700000000000000002",
                                                [_attr("model", _str("ok-2"))],
                                            ),
                                        ],
                                    },
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        self.assertEqual(ingest.ingest_metrics(payload), (3, 0))

        rows = store.query_dicts(
            "SELECT model, attr_type, attrs FROM metric_points ORDER BY id"
        )
        self.assertEqual([r["model"] for r in rows], ["ok-1", None, "ok-2"])
        self.assertIsNone(rows[1]["attr_type"])


if __name__ == "__main__":
    unittest.main()
