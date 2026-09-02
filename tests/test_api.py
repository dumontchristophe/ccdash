# Run: python3 -m unittest discover -s tests -v  (from repo root)
import json
import os
import re
import statistics
import sys
import time
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base import BaseDBTest

from ccdash import api, ingest
from ccdash.core import store
from ccdash.core.aggregates import WEIGHTS
from ccdash.core.request import Filters, NotFoundError, Scope
from ccdash.pages import sessions
from ccdash.pages.analysis import (
    ANALYSIS_CAPS,
    api_analysis,
    api_calls,
    delegation_types,
)
from ccdash.pages.costs import api_costs, api_projects
from ccdash.pages.details import api_event, api_prompt, api_subagent
from ccdash.pages.health import HOOK_MAX_FIRES, api_health, api_hook
from ccdash.pages.overview import api_filters, api_overview
from ccdash.pages.sessions import (
    RESPONSE_CLIP,
    api_context,
    api_session,
    api_sessions,
)

# The seeds mirror what Claude Code exports: a tool_result sends its numbers as
# strings, an api_request as integers, and only is_async and is_built_in are real
# booleans. Inventing a shape here would test a payload nobody sends.


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


def now_ns(seconds_ago=0):
    """`seconds_ago` places a seed outside the window a filter keeps, which is
    what tells the two halves of a doubly-substituted clause apart."""
    return str(int((time.time() - seconds_ago) * 1_000_000_000))


NO_FILTER = Filters(days=0, host=None, project=None)


def seed_metric(
    name,
    value,
    session_id,
    model="claude-opus-4-8",
    attr_type=None,
    host="testhost",
    project="testproj",
    query_source="main",
    seconds_ago=0,
):
    attrs = [
        _attr("session.id", _str(session_id)),
        _attr("model", _str(model)),
        _attr("host", _str(host)),
        _attr("project", _str(project)),
        _attr("query_source", _str(query_source)),
    ]
    if attr_type:
        attrs.append(_attr("type", _str(attr_type)))
    payload = {
        "resourceMetrics": [
            {
                "resource": {"attributes": []},
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": name,
                                "sum": {
                                    "aggregationTemporality": "AGGREGATION_TEMPORALITY_DELTA",
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": now_ns(seconds_ago),
                                            "asDouble": float(value),
                                            "attributes": attrs,
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


def seed_log_event(
    event_name, session_id, attributes=None, host="testhost", seconds_ago=0, ts=None
):
    """`ts` pins the epoch second of the record instead of deriving it from now:
    two events a query pairs on their timestamp cannot be seeded a fraction of a
    second apart and hope to land in the same second."""
    base = [
        _attr("event.name", _str("claude_code." + event_name)),
        _attr("session.id", _str(session_id)),
        _attr("host", _str(host)),
        _attr("project", _str("testproj")),
    ]
    if attributes:
        base.extend(attributes)
    payload = {
        "resourceLogs": [
            {
                "resource": {"attributes": []},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": (
                                    str(int(ts) * 1_000_000_000)
                                    if ts is not None
                                    else now_ns(seconds_ago)
                                ),
                                "attributes": base,
                            }
                        ]
                    }
                ],
            }
        ]
    }
    ingest.ingest_logs(payload)


class TestApiWithSeedData(BaseDBTest):
    """
    Tests of the API endpoints against representative seeded data.

    Two sessions, two models, several events, one project.
    """

    SID1 = "session-alpha"
    SID2 = "session-beta"
    MODEL1 = "claude-opus-4-8"
    MODEL2 = "claude-sonnet-4-6"

    def setUp(self):
        super().setUp()
        self._seed()

    def _seed(self):
        # Session 1: Opus.
        seed_metric("claude_code.token.usage", 1000, self.SID1, self.MODEL1, "input")
        seed_metric("claude_code.token.usage", 200, self.SID1, self.MODEL1, "output")
        seed_metric("claude_code.token.usage", 500, self.SID1, self.MODEL1, "cacheRead")
        seed_metric(
            "claude_code.token.usage", 100, self.SID1, self.MODEL1, "cacheCreation"
        )
        seed_metric("claude_code.cost.usage", 0.05, self.SID1, self.MODEL1)
        seed_metric("claude_code.session.count", 1, self.SID1, self.MODEL1)
        seed_metric(
            "claude_code.lines_of_code.count", 30, self.SID1, self.MODEL1, "added"
        )
        seed_metric(
            "claude_code.lines_of_code.count", 10, self.SID1, self.MODEL1, "removed"
        )

        # Session 2: Sonnet.
        seed_metric("claude_code.token.usage", 500, self.SID2, self.MODEL2, "input")
        seed_metric("claude_code.token.usage", 100, self.SID2, self.MODEL2, "output")
        seed_metric("claude_code.cost.usage", 0.03, self.SID2, self.MODEL2)
        seed_metric("claude_code.session.count", 1, self.SID2, self.MODEL2)

        # Events: tool_result.
        # Session 1: 3 Bash (2 successes, 1 failure)
        for _ in range(2):
            seed_log_event(
                "tool_result",
                self.SID1,
                [
                    _attr("tool_name", _str("Bash")),
                    _attr("success", _str("true")),
                    _attr("duration_ms", _str("50")),
                    _attr("tool_result_size_bytes", _str("300")),
                    _attr("prompt.id", _str("p-001")),
                ],
            )
        seed_log_event(
            "tool_result",
            self.SID1,
            [
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("false")),
                _attr("error_type", _str("ShellError")),
                _attr("duration_ms", _str("10")),
                _attr("tool_result_size_bytes", _str("50")),
                _attr("prompt.id", _str("p-001")),
            ],
        )

        # Session 2: 1 Edit
        seed_log_event(
            "tool_result",
            self.SID2,
            [
                _attr("tool_name", _str("Edit")),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("30")),
                _attr("tool_result_size_bytes", _str("100")),
                _attr("prompt.id", _str("p-002")),
            ],
        )

        # user_prompt.
        seed_log_event(
            "user_prompt",
            self.SID1,
            [
                _attr("prompt", _str("Fix the bug")),
                _attr("prompt.id", _str("p-001")),
            ],
        )
        seed_log_event(
            "user_prompt",
            self.SID2,
            [
                _attr("prompt", _str("Refactor the module")),
                _attr("prompt.id", _str("p-002")),
            ],
        )

        # api_request.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("cost_usd", _dbl(0.05)),
                _attr("input_tokens", _int(1000)),
                _attr("output_tokens", _int(200)),
                _attr("model", _str(self.MODEL1)),
                _attr("query_source", _str("repl_main_thread")),
                _attr("prompt.id", _str("p-001")),
            ],
        )
        seed_log_event(
            "api_request",
            self.SID2,
            [
                _attr("cost_usd", _dbl(0.03)),
                _attr("input_tokens", _int(500)),
                _attr("output_tokens", _int(100)),
                _attr("model", _str(self.MODEL2)),
                _attr("query_source", _str("repl_main_thread")),
                _attr("prompt.id", _str("p-002")),
            ],
        )

    def test_the_overview_kpis_are_the_seeded_figures(self):
        # 3 Bash (session 1) + 1 Edit (session 2) = 4 calls. Tokens: session 1
        # 1000+200+500+100 = 1800, session 2 500+100 = 600, total 2400.
        kpi = api_overview(NO_FILTER)["kpi"]
        self.assertEqual(kpi["sessions"], 2)
        self.assertEqual(kpi["tool_calls"], 4)
        self.assertEqual(kpi["prompts"], 2)
        self.assertEqual(kpi["tokens"], 2400)
        self.assertAlmostEqual(kpi["cost"], 0.08, places=5)

    def test_overview_tokens_by_type(self):
        result = api_overview(NO_FILTER)
        tok = result["tokens"]
        self.assertEqual(tok.get("input", 0), 1500)  # 1000 + 500
        self.assertEqual(tok.get("output", 0), 300)  # 200 + 100
        self.assertEqual(tok.get("cache_read", 0), 500)
        self.assertEqual(tok.get("cache_creation", 0), 100)

    def test_the_otlp_spelling_stops_at_the_payload(self):
        """metric_points stores `cacheRead` / `cacheCreation` -- the OTLP spelling, and
        a production database is never rewritten. Every payload the browser reads names
        the same four types in snake_case, so no consumer has to know both."""
        costs = api_costs(NO_FILTER)
        self.assertEqual(costs["tokens"].get("cache_read"), 500)
        self.assertEqual(costs["tokens"].get("cache_creation"), 100)
        self.assertEqual(
            set(costs["weights"]), {"input", "cache_read", "cache_creation", "output"}
        )
        camel = re.compile(r"[a-z]+[A-Z]")
        session = api_session(self.SID1)
        keyed_by_type = []
        for payload in (costs, api_overview(NO_FILTER), session):
            keyed_by_type += [
                payload.get("tokens"),
                payload.get("weights"),
                payload.get("weighted"),
                (payload.get("head") or {}).get("weights"),
                (payload.get("head") or {}).get("weighted"),
            ]
        # The two other dicts a browser reads by token type: one model family
        # of `/api/costs.per_model`, and the per-origin totals of a session.
        keyed_by_type += list(costs["per_model"].values())
        keyed_by_type += [s["tokens"] for s in session["sources"]]
        self.assertEqual(costs["per_model"]["Opus"]["cache_read"], 500)
        main = next(s for s in session["sources"] if s["source"] == "main")
        self.assertEqual(main["tokens"]["cache_read"], 500)
        for dicts in keyed_by_type:
            for key in dicts or {}:
                self.assertIsNone(camel.search(key), key)

    def test_overview_models_are_counted_not_costed(self):
        # Calls, not dollars: Haiku answers often for next to nothing, which a
        # cost breakdown hides, and the Costs page already reports spending.
        calls = api_overview(NO_FILTER)["model_calls"]
        self.assertEqual(calls, {"Opus": 1, "Sonnet": 1})
        self.assertNotIn("models", api_overview(NO_FILTER))

    def test_the_type_breakdown_counts_the_whole_population(self):
        # Grouped in SQL over the window, never counted from a list of rows.
        # 320 delegations: a breakdown built from a page of them would report a
        # slice, and the ratio between the two types with it.
        now = int(time.time())
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts, name, session_id, project, host, tool_name, "
                "agent_type, duration_ms, result_bytes, success) "
                "VALUES (?, 'tool_result', 'cap-session', 'testproj', 'testhost', 'Agent', "
                "?, 1000, 10, 'true')",
                [(now, "Explore" if i % 4 else "Plan") for i in range(320)],
            )
        try:
            counted = {
                d["agent_type"]: d["calls"] for d in delegation_types(Scope.UNBOUNDED)
            }
            self.assertEqual(counted["Explore"], 240)
            self.assertEqual(counted["Plan"], 80)
        finally:
            with store.write() as db:
                db.execute("DELETE FROM events WHERE session_id='cap-session'")

    def test_no_previous_window_when_the_filter_is_all(self):
        # days=0 drops the time clause entirely: the window already covers
        # everything, so there is no earlier one to compare it against.
        self.assertIsNone(api_overview(NO_FILTER)["prev"])

    def test_no_previous_window_when_it_holds_nothing(self):
        # Every seeded row is stamped now, so days 14 to 7 back are empty.
        # Comparing against nothing would report a plunge to -100%.
        self.assertIsNone(api_overview(replace(NO_FILTER, days=7))["prev"])

    def test_previous_window_carries_the_earlier_figures(self):
        # One row ten days back: inside the 14-to-7 window, outside the last 7.
        with store.write() as db:
            db.execute(
                "INSERT INTO metric_points (ts, name, value, session_id, project, host) "
                "VALUES (?, 'claude_code.cost.usage', 5.0, 'older-session', 'testproj', 'testhost')",
                (int(time.time()) - 10 * 86400,),
            )
        try:
            prev = api_overview(replace(NO_FILTER, days=7))["prev"]
            self.assertEqual(prev["sessions"], 1)
            self.assertAlmostEqual(prev["cost"], 5.0, places=5)
            # The window moved, it did not widen: today's rows stay out of it.
            self.assertEqual(prev["tool_calls"], 0)
        finally:
            with store.write() as db:
                db.execute("DELETE FROM metric_points WHERE session_id='older-session'")

    def test_overview_rhythm_is_a_7x24_grid(self):
        rhythm = api_overview(NO_FILTER)["rhythm"]
        self.assertEqual(len(rhythm), 7)
        self.assertTrue(all(len(row) == 24 for row in rhythm))

    def test_overview_rhythm_agrees_with_python_localtime(self):
        # Two independent derivations of the same cell, SQLite's strftime against
        # Python's localtime: a divergence is the day-shift the heatmap is exposed
        # to, and it only shows up around a DST boundary.
        expected = {}
        for r in store.query("SELECT ts FROM metric_points"):
            lt = time.localtime(r["ts"])
            cell = (lt.tm_wday, lt.tm_hour)
            expected[cell] = expected.get(cell, 0) + 1
        self.assertTrue(
            expected, "no metric point seeded: the comparison would be vacuous"
        )
        rhythm = api_overview(NO_FILTER)["rhythm"]
        got = {
            (d, h): n for d, row in enumerate(rhythm) for h, n in enumerate(row) if n
        }
        self.assertEqual(got, expected)

    def test_a_session_row_carries_the_seeded_figures_of_its_own_session(self):
        # One row per seeded session, each naming itself, its tools, its tokens
        # (1000+200+500+100 and 500+100), its model family and its project.
        rows = api_sessions(NO_FILTER)["sessions"]
        self.assertEqual(len(rows), 2)
        by_sid = {s["session_id"]: s for s in rows}
        self.assertEqual(sorted(by_sid), sorted((self.SID1, self.SID2)))
        self.assertEqual(by_sid[self.SID1]["tools"], 3)
        self.assertEqual(by_sid[self.SID2]["tools"], 1)
        self.assertEqual(by_sid[self.SID1]["tokens"], 1800)
        self.assertEqual(by_sid[self.SID2]["tokens"], 600)
        self.assertIn("Opus", by_sid[self.SID1]["models"])
        self.assertIn("Sonnet", by_sid[self.SID2]["models"])
        for s in rows:
            self.assertEqual(s["project"], "testproj")

    def test_the_output_share_is_taken_on_the_weight_not_on_the_raw_counts(self):
        by_sid = {s["session_id"]: s for s in api_sessions(NO_FILTER)["sessions"]}
        s = by_sid[self.SID1]
        # 1000 input, 500 cache_read, 100 cache_creation, 200 output ->
        # 1000 + 50 + 125 + 1000 = 2175 units of weight, half of them output.
        self.assertAlmostEqual(s["output_weight_pct"], 100 * 1000 / 2175, places=6)
        # Raw, output is 200/1800 = 11%. The two figures are not the same
        # question and the bar reads the weighted one.
        self.assertGreater(
            s["output_weight_pct"], 100 * s["output_tokens"] / s["tokens"]
        )

    def test_the_session_medians(self):
        # Active time arrives as two typed points per session, 'cli' and 'user';
        # the session figures sum them without looking at the type.
        seed_metric("claude_code.active_time.total", 400, self.SID1, self.MODEL1, "cli")
        seed_metric(
            "claude_code.active_time.total", 200, self.SID1, self.MODEL1, "user"
        )
        seed_metric("claude_code.active_time.total", 150, self.SID2, self.MODEL2, "cli")
        seed_metric("claude_code.active_time.total", 50, self.SID2, self.MODEL2, "user")
        stats = api_sessions(NO_FILTER)["median"]
        self.assertEqual(stats["sessions"], 2)
        # An even count: the middle two averaged.
        self.assertEqual(stats["active_seconds"], 400)
        self.assertEqual(stats["tokens"], 1200)
        self.assertAlmostEqual(stats["cost"], 0.04)

    def test_the_median_output_share_is_the_middle_session_not_the_pooled_weight(self):
        # 1000 of 2175 weighted units on session 1, 500 of 1000 on session 2. The
        # median is the middle of those two shares, not the share of the summed
        # weights (1500/3175), which a few long sessions would carry.
        stats = api_sessions(NO_FILTER)["median"]
        self.assertAlmostEqual(stats["output_weight_pct"], (100 * 1000 / 2175 + 50) / 2)
        self.assertNotAlmostEqual(
            stats["output_weight_pct"], 100 * 1500 / 3175, places=1
        )

    def test_active_seconds_is_on_the_same_scale_as_the_session_span(self):
        # started_at and ended_at are epoch seconds, so the span they bound is
        # in seconds too, and a session cannot be active longer than it lasted.
        # A key carrying milliseconds under that name would miss by 1000x.
        start = int(time.time()) - 3600
        with store.write() as db:
            db.executemany(
                "INSERT INTO metric_points (ts, name, value, session_id, host, project) "
                "VALUES (?,?,?,'span-session','testhost','testproj')",
                [
                    (start, "claude_code.active_time.total", 1200.0),
                    (start + 3600, "claude_code.active_time.total", 600.0),
                    (start, "claude_code.cost.usage", 1.0),
                ],
            )
        try:
            by_sid = {s["session_id"]: s for s in api_sessions(NO_FILTER)["sessions"]}
            session = by_sid["span-session"]
            self.assertEqual(session["active_seconds"], 1800)
            self.assertEqual(session["ended_at"] - session["started_at"], 3600)
            self.assertLessEqual(
                session["active_seconds"], session["ended_at"] - session["started_at"]
            )
        finally:
            with store.write() as db:
                db.execute("DELETE FROM metric_points WHERE session_id='span-session'")

    def test_a_session_without_active_time_counts_as_zero(self):
        seed_metric("claude_code.active_time.total", 400, self.SID1, self.MODEL1, "cli")
        seed_metric(
            "claude_code.active_time.total", 200, self.SID1, self.MODEL1, "user"
        )
        stats = api_sessions(NO_FILTER)["median"]
        self.assertEqual(stats["active_seconds"], 300)

    def test_the_list_is_not_capped_and_the_medians_match_it(self):
        # No page cut: every spending session ships, so the list length equals
        # `stats.sessions` and its median equals the one `stats` reports. Each
        # gets a token, or it would read as idle and be dropped before the
        # medians.
        with store.write() as db:
            db.executemany(
                "INSERT INTO metric_points (ts, name, value, session_id, host, project, "
                "attr_type, query_source, attrs) VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        1_700_000_000 + i,
                        name,
                        value,
                        "bulk-%03d" % i,
                        "testhost",
                        "testproj",
                        attr_type,
                        "main",
                        "{}",
                    )
                    for i in range(600)
                    for name, value, attr_type in (
                        (
                            "claude_code.active_time.total",
                            10.0 if i >= 400 else 1000.0,
                            None,
                        ),
                        ("claude_code.token.usage", 1.0, "input"),
                    )
                ],
            )
        result = api_sessions(NO_FILTER)
        self.assertEqual(len(result["sessions"]), 602)
        self.assertEqual(result["median"]["sessions"], 602)
        self.assertEqual(result["median"]["active_seconds"], 1000)
        self.assertEqual(
            statistics.median([s["active_seconds"] for s in result["sessions"]]), 1000
        )

    def test_no_sessions_gives_zeroed_medians(self):
        stats = api_sessions(replace(NO_FILTER, host="nosuchhost"))["median"]
        self.assertEqual(
            stats,
            {
                "sessions": 0,
                "idle": 0,
                "active_seconds": 0,
                "prompts": 0,
                "tokens": 0,
                "cost": 0,
                "output_weight_pct": 0,
            },
        )

    def test_costs_are_broken_down_per_model_family(self):
        # One entry per family seeded, each carrying the spending and the token
        # counts of that family alone.
        per_model = api_costs(NO_FILTER)["per_model"]
        self.assertEqual(sorted(per_model), ["Opus", "Sonnet"])
        opus = per_model["Opus"]
        self.assertAlmostEqual(opus.get("cost", 0), 0.05, places=5)
        self.assertEqual(opus.get("input", 0), 1000)
        self.assertEqual(opus.get("output", 0), 200)

    def test_costs_weighted_present(self):
        result = api_costs(NO_FILTER)
        self.assertIn("weighted", result)
        w = result["weighted"]
        # input*1.0 + output*5.0 + cache_read*0.1 + cache_creation*1.25
        expected = 1500 * 1.0 + 300 * 5.0 + 500 * 0.1 + 100 * 1.25
        self.assertAlmostEqual(
            w.get("input", 0)
            + w.get("output", 0)
            + w.get("cache_read", 0)
            + w.get("cache_creation", 0),
            expected,
            places=3,
        )

    def test_health_prompts_total(self):
        result = api_health(NO_FILTER)
        self.assertEqual(result["prompts_total"], 2)

    def test_a_project_row_carries_the_seeded_figures(self):
        # The one project the fixture seeds, with the two sessions, the four
        # tool calls and the spending of both of them.
        proj = next(r for r in api_projects(NO_FILTER) if r["project"] == "testproj")
        self.assertEqual(proj["sessions"], 2)
        self.assertEqual(proj["tools"], 4)
        self.assertAlmostEqual(proj["cost"], 0.08, places=5)

    def test_a_project_with_no_tool_call_reports_zero_rather_than_dropping_out(self):
        # The tool counts are looked up per project rather than asked per
        # project, and a project the lookup has no key for is the case that
        # turns a 0 into a missing field.
        seed_metric("claude_code.cost.usage", 0.01, "sid-quiet", project="quietproj")
        proj = next(r for r in api_projects(NO_FILTER) if r["project"] == "quietproj")
        self.assertEqual(proj["tools"], 0)
        self.assertEqual(proj["models"], ["Opus"])

    def test_a_model_seen_only_off_the_main_thread_does_not_chip_a_project(self):
        seed_metric(
            "claude_code.token.usage",
            10,
            self.SID1,
            "claude-haiku-4-5",
            "input",
            query_source="auxiliary",
        )
        proj = next(r for r in api_projects(NO_FILTER) if r["project"] == "testproj")
        self.assertNotIn("Haiku", proj["models"])

    def test_filters_offer_a_project_seen_only_in_the_events(self):
        # Metrics and logs are separate exports, and every analysis view reads
        # events: a project seen only there still has to be selectable, next to
        # the host and the project the metrics named.
        with store.write() as db:
            db.execute(
                "INSERT INTO events (ts, name, session_id, host, project) "
                "VALUES (?, 'tool_result', 'logs-only', 'loghost', 'logsproj')",
                (int(time.time()),),
            )
        result = api_filters()
        self.assertIn("testhost", result["hosts"])
        self.assertIn("testproj", result["projects"])
        self.assertIn("logsproj", result["projects"])
        self.assertIn("loghost", result["hosts"])
        # Still there once, not once per table.
        self.assertEqual(result["projects"].count("testproj"), 1)

    def test_calls_returns_one_row_per_call_of_the_named_tool(self):
        result = api_calls("Bash", NO_FILTER)
        self.assertEqual(len(result), 3)
        # Each call carries its event id so the drill-down row can open the
        # inspector, and the session it was made in.
        self.assertTrue(all("id" in r for r in result))
        self.assertEqual({r["session_id"] for r in result}, {self.SID1})
        self.assertEqual(len(api_calls("Edit", NO_FILTER)), 1)

    def test_calls_ship_the_file_a_read_targeted(self):
        """The drill-down table names the file, so the column has to come down
        with the row: `params` is not selected here and `attrs` deliberately
        never is, which leaves the column as the only path to it."""
        seed_log_event(
            "tool_result",
            self.SID1,
            [
                _attr("tool_name", _str("Read")),
                _attr("success", _str("true")),
                _attr(
                    "tool_input",
                    _str(json.dumps({"file_path": "/w/proj/app/ccdash.py"})),
                ),
            ],
        )
        result = api_calls("Read", NO_FILTER)
        self.assertEqual([r["file_path"] for r in result], ["/w/proj/app/ccdash.py"])

    def test_calls_of_a_tool_without_a_file_still_carry_the_key(self):
        # The column is dropped by the modal when no row fills it, which reads
        # the key on every row: absent rather than None would throw instead.
        self.assertTrue(all("file_path" in r for r in api_calls("Bash", NO_FILTER)))

    def test_calls_unknown_label_empty(self):
        result = api_calls("NonExistent", NO_FILTER)
        self.assertEqual(result, [])

    def test_calls_session_scoped(self):
        # Global returns Bash calls from both sessions; scoping to SID1 restricts them.
        all_calls = api_calls("Bash", NO_FILTER)
        scoped = api_calls("Bash", NO_FILTER, session=self.SID1)
        self.assertTrue(all(r["session_id"] == self.SID1 for r in scoped))
        self.assertLessEqual(len(scoped), len(all_calls))

    def _seed_file_call(self, tool, path, success="true"):
        seed_log_event(
            "tool_result",
            self.SID1,
            [
                _attr("tool_name", _str(tool)),
                _attr("success", _str(success)),
                _attr("tool_input", _str(json.dumps({"file_path": path}))),
            ],
        )

    def test_calls_can_be_scoped_to_a_file_across_tools(self):
        # The Files tab opens on a path, not on a tool: an Edit and a Write on
        # the same file belong to the same drill-down.
        self._seed_file_call("Edit", "/w/proj/a.py")
        self._seed_file_call("Write", "/w/proj/a.py")
        self._seed_file_call("Edit", "/w/proj/b.py")
        result = api_calls("", NO_FILTER, file="/w/proj/a.py")
        self.assertEqual(sorted(r["label"] for r in result), ["Edit", "Write"])

    def test_a_file_drill_down_counts_what_the_files_tab_counted(self):
        # Both sides restrict to Edit and Write, so the figure on the row and
        # the length of the list it opens are the same number.
        self._seed_file_call("Edit", "/w/proj/a.py")
        self._seed_file_call("Write", "/w/proj/a.py")
        self._seed_file_call("Read", "/w/proj/a.py")
        row = next(
            f
            for f in api_session(self.SID1)["files"]
            if f["file_path"] == "/w/proj/a.py"
        )
        self.assertEqual(
            row["calls"], len(api_calls("", NO_FILTER, file="/w/proj/a.py"))
        )

    def test_calls_take_a_label_and_a_file_together(self):
        self._seed_file_call("Edit", "/w/proj/a.py")
        self._seed_file_call("Write", "/w/proj/a.py")
        result = api_calls("Write", NO_FILTER, file="/w/proj/a.py")
        self.assertEqual([r["label"] for r in result], ["Write"])

    def test_calls_with_neither_a_label_nor_a_file_answer_nothing(self):
        # Both empty would otherwise select the 200 most recent calls of any
        # tool, which no row on the page asks for.
        self.assertEqual(api_calls("", NO_FILTER), [])

    def test_calls_normalise_success_to_a_bool_at_the_boundary(self):
        # OTEL sends the string "true"/"false"; the payload carries a real bool.
        self._seed_file_call("Edit", "/w/proj/ok.py", success="true")
        self._seed_file_call("Edit", "/w/proj/ko.py", success="false")
        by_file = {
            r["file_path"]: r["success"]
            for r in api_calls("Edit", NO_FILTER, file="/w/proj/ok.py")
        }
        by_file.update(
            {
                r["file_path"]: r["success"]
                for r in api_calls("Edit", NO_FILTER, file="/w/proj/ko.py")
            }
        )
        self.assertIs(by_file["/w/proj/ok.py"], True)
        self.assertIs(by_file["/w/proj/ko.py"], False)

    def test_an_event_normalises_success_to_a_bool(self):
        self._seed_file_call("Edit", "/w/proj/ko.py", success="false")
        eid = api_calls("Edit", NO_FILTER, file="/w/proj/ko.py")[0]["id"]
        self.assertIs(api_event(eid)["success"], False)

    def test_an_event_ships_its_call_arguments_parsed(self):
        # `tool_input` is stored as an escaped JSON string inside `attrs`. The
        # Edit and Write panels read fields out of it, so it comes down parsed.
        self._seed_file_call("Edit", "/w/proj/a.py")
        eid = api_calls("Edit", NO_FILTER, file="/w/proj/a.py")[0]["id"]
        self.assertEqual(api_event(eid)["tool_input"]["file_path"], "/w/proj/a.py")

    def test_an_event_with_no_call_arguments_ships_none(self):
        eid = api_calls("Bash", NO_FILTER)[0]["id"]
        self.assertIsNone(api_event(eid)["tool_input"])

    def test_session_detail_head_cost(self):
        result = api_session(self.SID1)
        self.assertAlmostEqual(result["head"]["cost"], 0.05, places=5)

    def test_session_detail_tools(self):
        result = api_session(self.SID1)
        tool_labels = {t["label"] for t in result["tools"]}
        self.assertIn("Bash", tool_labels)

    def test_session_detail_errors(self):
        result = api_session(self.SID1)
        errors = result["errors"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["label"], "Bash")
        self.assertEqual(errors[0]["error_type"], "ShellError")

    def test_session_detail_prompts(self):
        result = api_session(self.SID1)
        prompts = result["prompts"]
        self.assertEqual(len(prompts), 1)
        p = prompts[0]
        self.assertEqual(p["prompt_id"], "p-001")
        self.assertEqual(p["tools"], 3)
        self.assertEqual(p["session_id"], self.SID1)

    def test_session_detail_tokens(self):
        result = api_session(self.SID1)
        h = result["head"]
        self.assertEqual(h["input_tokens"], 1000)
        self.assertEqual(h["output_tokens"], 200)

    def test_the_session_list_and_the_session_detail_agree_on_the_models(self):
        # The auxiliary calls Claude Code makes on its own run on Haiku. Both
        # views exclude them, so a session carries the same chip in the list
        # and on its own page.
        seed_metric(
            "claude_code.token.usage",
            10,
            self.SID1,
            "claude-haiku-4-5-20251001",
            "input",
            query_source="auxiliary",
        )
        listed = next(
            s
            for s in api_sessions(NO_FILTER)["sessions"]
            if s["session_id"] == self.SID1
        )
        self.assertEqual(api_session(self.SID1)["head"]["models"], listed["models"])
        self.assertNotIn("Haiku", listed["models"])

    def test_the_session_head_weights_its_tokens_like_the_costs_page(self):
        # The bar in the session header reads `weighted`, not the raw counts:
        # re-reading is most of the volume and a tenth of the weight, so raw
        # counts would draw one solid cache_read block on every session.
        h = api_session(self.SID1)["head"]
        self.assertEqual(h["weights"], WEIGHTS)
        for k in WEIGHTS:
            self.assertAlmostEqual(
                h["weighted"][k], h[k + "_tokens"] * WEIGHTS[k], places=6
            )

    def test_the_context_curve_is_the_whole_prompt_of_a_request(self):
        # Fresh input, cache reads and cache writes summed. The request seeded
        # in setUp carries fresh input alone, hence the two equal points.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(40)),
                _attr("cache_read_tokens", _int(900)),
                _attr("cache_creation_tokens", _int(60)),
                _attr("query_source", _str("repl_main_thread")),
            ],
        )
        curve = api_session(self.SID1)["context"]
        self.assertEqual(sorted(p["value"] for p in curve), [1000, 1000])

    def test_the_context_curve_leaves_out_what_is_not_the_main_thread(self):
        # A sub-agent holds a context of its own: on this curve it would read
        # as a spike the session never went through.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(99999)),
                _attr("query_source", _str("agent:builtin:Explore")),
            ],
        )
        self.assertEqual(
            [p["value"] for p in api_session(self.SID1)["context"]], [1000]
        )

    def test_the_context_curve_answers_to_both_spellings_of_the_main_thread(self):
        # Current builds send repl_main_thread, older ones sent main.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(7)),
                _attr("query_source", _str("main")),
            ],
        )
        self.assertEqual(
            sorted(p["value"] for p in api_session(self.SID1)["context"]), [7, 1000]
        )

    def test_the_context_curve_reads_an_agent_sdk_session(self):
        # A session driven by the Claude Agent SDK names its main thread `sdk`.
        # The events are the same conversation; left out, the card claimed no
        # request had been recorded on sessions holding dozens.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(11)),
                _attr("cache_read_tokens", _int(500)),
                _attr("query_source", _str("sdk")),
            ],
        )
        self.assertEqual(
            sorted(p["value"] for p in api_session(self.SID1)["context"]),
            [511, 1000],
        )

    def test_the_head_lists_the_output_styles_the_session_ran_under(self):
        # Distinct, not the last one: a session where the style was switched
        # mid-run shows both, the same honesty as the Models row.
        for style in ("Concise", "Explanatory", "Concise"):
            seed_log_event(
                "api_request",
                self.SID1,
                [
                    _attr(
                        "query_source", _str("repl_main_thread:outputStyle:" + style)
                    ),
                ],
            )
        self.assertEqual(
            api_session(self.SID1)["head"]["output_styles"],
            ["Concise", "Explanatory"],
        )

    def test_a_session_that_set_no_style_lists_none(self):
        # Claude Code omits the suffix on the default style, so absence is the
        # fact -- there is no `outputStyle:Default` to match and none is invented.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("query_source", _str("repl_main_thread")),
            ],
        )
        self.assertEqual(api_session(self.SID1)["head"]["output_styles"], [])

    def test_the_head_lists_the_efforts_the_session_ran_under(self):
        # `effort` is a plain top-level attribute, never part of query_source,
        # and has no column: it is read out of the raw attrs blob.
        for effort in ("high", "medium", "high"):
            seed_log_event(
                "api_request",
                self.SID1,
                [
                    _attr("query_source", _str("repl_main_thread")),
                    _attr("effort", _str(effort)),
                ],
            )
        self.assertEqual(api_session(self.SID1)["head"]["efforts"], ["high", "medium"])

    def test_a_session_predating_the_effort_attribute_lists_none(self):
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("query_source", _str("repl_main_thread")),
            ],
        )
        self.assertEqual(api_session(self.SID1)["head"]["efforts"], [])

    def test_the_context_curve_reads_a_request_made_under_an_output_style(self):
        # Claude Code appends `:outputStyle:<name>` to query_source when a style
        # is in force. The curve is about the main thread, and a styled main
        # thread is still the main thread.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(13)),
                _attr("query_source", _str("repl_main_thread:outputStyle:Concise")),
            ],
        )
        self.assertEqual(
            sorted(p["value"] for p in api_session(self.SID1)["context"]),
            [13, 1000],
        )

    def test_a_namespaced_subagent_origin_survives_the_split(self):
        # Three segments and no style: splitting on `:` would turn it into `agent`,
        # and counting segments cannot tell the two shapes apart. The anchor is the
        # literal `:outputStyle:`, so this one is left whole.
        seed_log_event(
            "api_request",
            self.SID1,
            [
                _attr("input_tokens", _int(21)),
                _attr("query_source", _str("agent:builtin:general-purpose")),
            ],
        )
        row = store.query_row(
            "SELECT query_origin, output_style FROM events "
            "WHERE query_source='agent:builtin:general-purpose'"
        )
        self.assertEqual(row["query_origin"], "agent:builtin:general-purpose")
        self.assertIsNone(row["output_style"])
        self.assertEqual(
            [p["value"] for p in api_session(self.SID1)["context"]], [1000]
        )

    def test_session_unknown_raises_rather_than_answering_a_head_of_zeros(self):
        # The head is a set of aggregates: an id nothing was recorded under
        # answers a row of NULLs, which reads as a session that did nothing.
        with self.assertRaises(NotFoundError):
            api_session("no-such-session")

    def _seed_bash(self, n, first_ts=1_700_000_000):
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts, name, session_id, host, project, tool_name, label, "
                "bash_cmd, success, agent_type, attrs) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        first_ts + i,
                        "tool_result",
                        self.SID1,
                        "testhost",
                        "testproj",
                        "Bash",
                        "Bash",
                        "echo %d" % i,
                        "false",
                        "Explore",
                        "{}",
                    )
                    for i in range(n)
                ],
            )

    def test_a_hook_fire_carries_its_name_and_no_tool_label(self):
        # The name lives only in the attrs blob, which the timeline query does
        # not ship; the label column names the tool, and a hook has none.
        seed_log_event(
            "hook_execution_complete",
            self.SID1,
            [_attr("hook_name", _str("PreToolUse:Bash"))],
        )
        # A fire with no matcher behind it: Claude Code names the event it hung
        # off and nothing else, and `hook_event` is what the timeline falls back
        # to.
        seed_log_event(
            "hook_execution_complete",
            self.SID1,
            [_attr("hook_event", _str("UserPromptSubmit"))],
        )
        hooks = [
            e
            for e in api_session(self.SID1)["events"]
            if e["name"] == "hook_execution_complete"
        ]
        names = {e["hook_name"] for e in hooks}
        self.assertEqual(names, {"PreToolUse:Bash", "UserPromptSubmit"})
        self.assertEqual([e["label"] for e in hooks], [None, None])

    def test_an_assistant_response_reaches_the_timeline_clipped(self):
        """Same reason as the hook name: the text lives only in the attrs blob,
        which the timeline query does not ship. It is cut server-side because
        Claude Code sends up to 60 KB of it per turn."""
        long_answer = "x" * (RESPONSE_CLIP + 50)
        seed_log_event(
            "assistant_response",
            self.SID1,
            [
                _attr("response", _str(long_answer)),
                _attr("response_length", _int(len(long_answer))),
                _attr("prompt.id", _str("p-001")),
            ],
        )
        events = {e["name"]: e for e in api_session(self.SID1)["events"]}
        answer = events["assistant_response"]
        self.assertEqual(answer["response"], "x" * RESPONSE_CLIP)
        # The length Claude Code sent, not the length of what was kept: it is
        # what tells the row it was cut.
        self.assertEqual(answer["response_length"], len(long_answer))
        # A row of any other kind carries neither, and says so as null rather
        # than as an empty string.
        self.assertIsNone(events["user_prompt"]["response"])
        self.assertIsNone(events["user_prompt"]["response_length"])

    def test_the_timeline_ships_the_file_a_call_targeted(self):
        """Same reason as the drill-down, with one more: the timeline query is
        capped and explicitly leaves `attrs` behind because it would be
        megabytes on a long session. The column is the whole point."""
        seed_log_event(
            "tool_result",
            self.SID1,
            [
                _attr("tool_name", _str("Write")),
                _attr("success", _str("true")),
                _attr(
                    "tool_input", _str(json.dumps({"file_path": "/w/proj/notes.md"}))
                ),
            ],
        )
        events = api_session(self.SID1)["events"]
        writes = [e for e in events if e["tool_name"] == "Write"]
        self.assertEqual([e["file_path"] for e in writes], ["/w/proj/notes.md"])

    def test_the_session_lists_the_files_it_changed(self):
        self._seed_file_call("Edit", "/w/proj/a.py")
        self._seed_file_call("Edit", "/w/proj/a.py", success="false")
        self._seed_file_call("Write", "/w/proj/a.py")
        row = next(
            f
            for f in api_session(self.SID1)["files"]
            if f["file_path"] == "/w/proj/a.py"
        )
        self.assertEqual(
            (row["calls"], row["edits"], row["writes"], row["failures"]), (3, 2, 1, 1)
        )

    def test_a_file_that_was_only_read_is_not_listed_as_changed(self):
        self._seed_file_call("Read", "/w/proj/readonly.py")
        paths = {f["file_path"] for f in api_session(self.SID1)["files"]}
        self.assertNotIn("/w/proj/readonly.py", paths)

    def test_a_tool_call_keeps_its_label_and_has_no_hook_name(self):
        events = {e["id"]: e for e in api_session(self.SID1)["events"]}
        tools = [e for e in events.values() if e["name"] == "tool_result"]
        self.assertTrue(tools)
        for e in tools:
            self.assertIsNotNone(e["label"])
            self.assertIsNone(e["hook_name"])

    def test_subagents_is_the_subagent_completed_population_and_nothing_else(self):
        """`subagents` reports what the sub-agent said about itself, parsed out of
        the attrs blob. The delegations the caller saw -- Task tool_results -- are a
        different, larger population, and `/api/session` ships no list of them: the
        page renders `subagents` and nothing reads the other."""
        # Three rows carrying an agent type: delegations, not completions.
        self._seed_bash(3)
        seed_log_event(
            "subagent_completed",
            self.SID1,
            [
                _attr("agent_type", _str("Explore")),
                _attr("model", _str(self.MODEL1)),
                _attr("total_tokens", _int(4200)),
                _attr("total_tool_uses", _int(7)),
                _attr("duration_ms", _str("9000")),
            ],
        )
        result = api_session(self.SID1)
        for absent in ("agents", "delegations"):
            self.assertNotIn(absent, result)
        # One completion against three delegations in the same session.
        self.assertEqual(len(result["subagents"]), 1)
        self.assertEqual(
            sum(
                t["calls"]
                for t in delegation_types(Scope(" AND session_id=?", (self.SID1,)))
            ),
            3,
        )
        # Fields the SELECT cannot hand over: they are parsed from `attrs`.
        row = result["subagents"][0]
        self.assertEqual((row["tokens"], row["tools"]), (4200, 7))
        self.assertEqual(row["model"], "Opus")

    def test_a_session_detail_keeps_what_the_global_caps_would_drop(self):
        # The global lists stop between 200 and 500 rows. A session detail is
        # opened to read what happened in it, so those caps do not apply.
        self._seed_bash(600)
        result = api_session(self.SID1)
        self.assertEqual(len(result["bash"]), 600)
        self.assertEqual(len(result["errors"]), 601)
        self.assertEqual(result["truncated"], [])

    def test_a_runaway_session_is_cut_and_says_so(self):
        # The ceiling is a guard against freezing the tab, not a page size, so
        # what it drops has to be named rather than silently missing.
        self._seed_bash(80)
        self.addCleanup(
            setattr, sessions, "SESSION_MAX_ROWS", sessions.SESSION_MAX_ROWS
        )

        # A list that exactly fills the ceiling is full, not cut. The 80 seeded
        # rows are the only ones carrying a command or an agent type; the
        # fixture's own events push the timeline and the failures past it.
        sessions.SESSION_MAX_ROWS = 80
        truncated = api_session(self.SID1)["truncated"]
        self.assertEqual(sorted(truncated), ["errors", "events"])

        sessions.SESSION_MAX_ROWS = 50
        result = api_session(self.SID1)
        self.assertEqual(len(result["bash"]), 50)
        self.assertEqual(len(result["events"]), 50)
        self.assertEqual(sorted(result["truncated"]), ["bash", "errors", "events"])


class TestApiFilterEffect(BaseDBTest):
    """Tests that check the host/project filters do restrict the results."""

    SID1 = "sess-proj-a"
    SID2 = "sess-proj-b"

    def setUp(self):
        super().setUp()
        # Session in "project-A"
        seed_metric(
            "claude_code.token.usage",
            1000,
            self.SID1,
            "claude-opus-4-8",
            "input",
            host="host1",
            project="project-A",
        )
        seed_metric(
            "claude_code.cost.usage",
            0.05,
            self.SID1,
            "claude-opus-4-8",
            None,
            host="host1",
            project="project-A",
        )
        # Session in "project-B"
        seed_metric(
            "claude_code.token.usage",
            500,
            self.SID2,
            "claude-sonnet-4-6",
            "input",
            host="host2",
            project="project-B",
        )
        seed_metric(
            "claude_code.cost.usage",
            0.02,
            self.SID2,
            "claude-sonnet-4-6",
            None,
            host="host2",
            project="project-B",
        )

    def test_either_filter_restricts_the_session_list(self):
        for f, expected in (
            (replace(NO_FILTER, project="project-A"), self.SID1),
            (replace(NO_FILTER, host="host2"), self.SID2),
        ):
            with self.subTest(host=f.host, project=f.project):
                result = api_sessions(f)["sessions"]
                self.assertEqual([s["session_id"] for s in result], [expected])

    def test_the_project_filter_restricts_the_overview_kpis(self):
        result = api_overview(replace(NO_FILTER, project="project-A"))
        self.assertEqual(result["kpi"]["sessions"], 1)
        self.assertAlmostEqual(result["kpi"]["cost"], 0.05, places=5)


class TestCountersAndDurationsAreNamedPerEndpoint(BaseDBTest):
    """`n` carried six meanings and `dur` three units. The names now say which
    one, and the identities behind them still hold."""

    SID = "session-counters"
    PID = "p-counters"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("prompt.id", _str(self.PID)),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("120")),
                _attr("tool_result_size_bytes", _str("400")),
            ],
        )
        seed_log_event(
            "user_prompt",
            self.SID,
            [
                _attr("prompt.id", _str(self.PID)),
                _attr("prompt", _str("count things")),
            ],
        )
        seed_log_event(
            "compaction",
            self.SID,
            [
                _attr("pre_tokens", _int(90000)),
                _attr("post_tokens", _int(1000)),
                _attr("trigger", _str("auto")),
            ],
        )
        seed_metric("claude_code.token.usage", 1000, self.SID, attr_type="input")
        seed_metric("claude_code.cost.usage", 0.05, self.SID)

    def test_a_tool_row_counts_calls_and_averages_milliseconds(self):
        tool = api_analysis(NO_FILTER)["tools"][0]
        self.assertEqual(tool["calls"], 1)
        self.assertNotIn("n", tool)
        self.assertNotIn("events", tool)
        # AVG(duration_ms) of a single 120 ms call.
        self.assertAlmostEqual(tool["avg_duration_ms"], 120)
        self.assertNotIn("dur", tool)


class TestDurationSecondsIsPinnedOnBothProducers(BaseDBTest):
    """`duration_s` is written twice -- prompt_stats and api_prompt -- from the
    same two event timestamps. Pinning one leaves the other free to drift into
    milliseconds, which is the ambiguity the rename was meant to close."""

    SID = "session-span"
    PID = "p-span"
    SPAN = 300

    def setUp(self):
        super().setUp()
        seed_log_event(
            "user_prompt",
            self.SID,
            [
                _attr("prompt.id", _str(self.PID)),
                _attr("prompt", _str("measure the span")),
            ],
            seconds_ago=self.SPAN,
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("prompt.id", _str(self.PID)),
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("120")),
                _attr("tool_result_size_bytes", _str("400")),
            ],
        )

    def test_prompt_stats_reports_the_span_in_seconds(self):
        prompt = api_analysis(NO_FILTER)["prompts"][0]
        self.assertAlmostEqual(prompt["duration_s"], self.SPAN, delta=2)
        self.assertEqual(
            prompt["duration_s"], prompt["ended_at"] - prompt["started_at"]
        )

    def test_api_prompt_reports_the_same_span_in_the_same_unit(self):
        # The endpoint the prompt modal reads. It recomputes the span from its
        # own query, so agreeing with prompt_stats is the whole assertion:
        # in milliseconds it would be off by a factor of a thousand.
        detail = api_prompt(self.PID)
        self.assertAlmostEqual(detail["duration_s"], self.SPAN, delta=2)
        prompt = api_analysis(NO_FILTER)["prompts"][0]
        self.assertEqual(detail["duration_s"], prompt["duration_s"])


class TestFilterReachesBothHalvesOfADoubleSubstitution(BaseDBTest):
    """api_context and api_projects substitute the scope clause twice -- once
    in a sub-select, once in the outer query -- and pass its args twice.

    These two tests constrain the **outer** clause and the args pairing: on this
    fixture neither sub-select changes its result when its own clause goes, so
    dropping a clause without its args is what they catch (sqlite3 raises
    `Incorrect number of bindings`). The sub-select's own filtering is pinned by
    TestTheSubSelectHalfIsFilteredToo, which needs a session straddling the
    window to show it."""

    def _compaction(self, session, host):
        seed_log_event(
            "compaction",
            session,
            [
                _attr("pre_tokens", _int(90000)),
                _attr("post_tokens", _int(1000)),
                _attr("trigger", _str("auto")),
            ],
            host=host,
        )
        seed_metric(
            "claude_code.token.usage", 1000, session, attr_type="input", host=host
        )
        seed_metric("claude_code.cost.usage", 0.05, session, host=host)

    def setUp(self):
        super().setUp()
        self._compaction("sess-h1", "host1")
        self._compaction("sess-h2", "host2")

    def test_api_context_keeps_only_the_filtered_host(self):
        f = replace(NO_FILTER, days=7, host="host1")
        sessions = api_context(f)["sessions"]
        self.assertEqual([s["session_id"] for s in sessions], ["sess-h1"])

    def test_api_projects_keeps_only_the_filtered_host(self):
        f = replace(NO_FILTER, days=7, host="host1")
        projects = api_projects(f)
        self.assertEqual(sum(p["sessions"] for p in projects), 1)


class TestTheSubSelectHalfIsFilteredToo(BaseDBTest):
    """The sub-select of api_context and api_projects carries the same clause as
    the outer query, and dropping it leaks silently -- no binding error, just a
    session that should not be there.

    Showing it takes a session that straddles the window: `sess-lapsed` spent
    tokens a month ago and did nothing but check in this week. Inside a 7-day
    window it is idle and unspent; over all time it is neither. So the sub-select
    answers differently with and without its clause, which is the only shape in
    which the leak is visible.
    """

    WINDOW = replace(NO_FILTER, days=7)
    MONTH = 30 * 86400

    def setUp(self):
        super().setUp()
        seed_metric("claude_code.token.usage", 1000, "sess-active", attr_type="input")
        seed_metric(
            "claude_code.token.usage",
            1000,
            "sess-lapsed",
            attr_type="input",
            seconds_ago=self.MONTH,
        )
        # In-window, but neither token nor cost: it puts the session in
        # metric_points for this week without making it spend anything.
        seed_metric("claude_code.session.count", 1, "sess-lapsed")
        for session in ("sess-active", "sess-lapsed"):
            seed_log_event(
                "compaction",
                session,
                [
                    _attr("pre_tokens", _int(90000)),
                    _attr("post_tokens", _int(1000)),
                    _attr("trigger", _str("auto")),
                ],
            )

    def test_api_context_drops_a_session_idle_within_the_window(self):
        sessions = api_context(self.WINDOW)["sessions"]
        self.assertEqual([s["session_id"] for s in sessions], ["sess-active"])

    def test_api_projects_counts_only_sessions_that_spent_within_the_window(self):
        projects = api_projects(self.WINDOW)
        self.assertEqual(sum(p["sessions"] for p in projects), 1)


class TestApiProjectsNullProject(BaseDBTest):
    """Checks that api_projects handles sessions with no project (NULL)."""

    def setUp(self):
        super().setUp()
        # Metric with no project
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
                                            {
                                                "timeUnixNano": str(
                                                    int(time.time() * 1e9)
                                                ),
                                                "asInt": 100,
                                                "attributes": [
                                                    _attr(
                                                        "session.id",
                                                        _str("sess-noproject"),
                                                    ),
                                                    _attr("type", _str("input")),
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

    def test_undefined_filter_selects_null_project(self):
        f = replace(NO_FILTER, project="(undefined)")
        sids = [s["session_id"] for s in api_sessions(f)["sessions"]]
        self.assertEqual(sids, ["sess-noproject"])

    def test_undefined_offered_as_filter_option(self):
        self.assertIn("(undefined)", api_filters()["projects"])


class TestIdleSessions(BaseDBTest):
    """A session that started and spent nothing counts nowhere.

    Claude Code exports `session.count` as soon as a process comes up, so a run
    that registers its hooks and exits leaves a session behind. Counting those
    halves every median. The fixture holds one of them, two that did work, and
    one that only ever produced log events -- the last is the case the exclusion
    must not touch.
    """

    IDLE = "sess-idle"
    CHEAP = "sess-cheap"
    RICH = "sess-rich"
    LOGS_ONLY = "sess-logs-only"

    def setUp(self):
        super().setUp()
        seed_metric("claude_code.session.count", 1, self.IDLE)
        # A prompt was typed and the run interrupted before the first request:
        # it puts the session in the event tables without spending anything.
        seed_log_event("user_prompt", self.IDLE, [_attr("prompt", _str("never mind"))])

        for sid, cost in ((self.CHEAP, 0.05), (self.RICH, 0.15)):
            seed_metric("claude_code.session.count", 1, sid)
            seed_metric("claude_code.token.usage", 1000, sid, attr_type="input")
            seed_metric("claude_code.cost.usage", cost, sid)

        seed_log_event(
            "compaction",
            self.LOGS_ONLY,
            [
                _attr("trigger", _str("auto")),
                _attr("pre_tokens", _int(90000)),
                _attr("post_tokens", _int(1000)),
            ],
        )

    def test_the_session_list_drops_it_and_says_how_many(self):
        result = api_sessions(NO_FILTER)
        self.assertEqual(
            sorted(s["session_id"] for s in result["sessions"]), [self.CHEAP, self.RICH]
        )
        self.assertEqual(result["median"]["sessions"], 2)
        self.assertEqual(result["median"]["idle"], 1)

    def test_the_medians_are_taken_over_the_sessions_that_worked(self):
        # The two costs averaged. Counting the idle session would put a zero in
        # the sample and drop the median to 0.025.
        self.assertAlmostEqual(api_sessions(NO_FILTER)["median"]["cost"], 0.10)

    def test_the_overview_counts_the_same_sessions_as_the_list(self):
        # Two figures for one thing on two pages is how this started.
        self.assertEqual(api_overview(NO_FILTER)["kpi"]["sessions"], 2)

    def test_a_project_does_not_count_it_either(self):
        proj = next(p for p in api_projects(NO_FILTER) if p["project"] == "testproj")
        self.assertEqual(proj["sessions"], 2)

    def test_the_context_view_does_not_rank_it(self):
        sids = [s["session_id"] for s in api_context(NO_FILTER)["sessions"]]
        self.assertNotIn(self.IDLE, sids)

    def test_a_session_seen_only_in_the_events_is_not_idle_but_unknown(self):
        # It has no metric_point, so nothing says it spent nothing. The exclusion
        # drops what is known to be idle, never what is unmeasured: the other way
        # round would empty this view for anyone exporting logs without metrics.
        sids = [s["session_id"] for s in api_context(NO_FILTER)["sessions"]]
        self.assertIn(self.LOGS_ONLY, sids)

    def test_a_cost_without_a_token_breakdown_still_counts_as_work(self):
        seed_metric("claude_code.session.count", 1, "sess-cost-only")
        seed_metric("claude_code.cost.usage", 0.20, "sess-cost-only")
        result = api_sessions(NO_FILTER)
        self.assertIn("sess-cost-only", [s["session_id"] for s in result["sessions"]])
        self.assertEqual(result["median"]["idle"], 1)
        # It kept its row, so the output share has to answer something: no
        # token breakdown means no weight to take a share of, not a division.
        row = next(s for s in result["sessions"] if s["session_id"] == "sess-cost-only")
        self.assertEqual(row["output_weight_pct"], 0)

    def test_diagnostics_lists_the_sessions_left_out(self):
        self.assertEqual(
            [r["session_id"] for r in api_health(NO_FILTER)["idle"]], [self.IDLE]
        )


class TestTheSessionTitleOnPromptsSharingASecond(BaseDBTest):
    """Which prompt names a session when several land in the same epoch second.

    `ts` is a second, so the ordering `session_titles` reads is `ts, id`: the
    prompt ingested first names the session, and the last /rename ingested
    overrides it. Without the `id` tiebreak either one is arbitrary.
    """

    TIED = "sess-tied"
    RENAMED = "sess-renamed"
    TS = 1_750_000_000

    def title(self, session_id):
        head = api_session(session_id)["head"]
        return head["title"], head["title_src"]

    def prompt(self, session_id, text):
        seed_log_event(
            "user_prompt", session_id, [_attr("prompt", _str(text))], ts=self.TS
        )

    def test_the_first_prompt_of_the_second_names_the_session(self):
        seed_metric("claude_code.cost.usage", 0.1, self.TIED)
        self.prompt(self.TIED, "first in, first named")
        self.prompt(self.TIED, "second in the same second")
        self.assertEqual(self.title(self.TIED), ("first in, first named", "prompt"))

    def test_the_last_rename_of_the_second_wins_over_the_prompts(self):
        seed_metric("claude_code.cost.usage", 0.1, self.RENAMED)
        self.prompt(self.RENAMED, "the prompt it opened on")
        self.prompt(self.RENAMED, "/rename first name")
        self.prompt(self.RENAMED, "/rename last name")
        self.assertEqual(self.title(self.RENAMED), ("last name", "rename"))


class TestTheSessionDetailReadsItsOwnSessionOnly(BaseDBTest):
    """How many rows `api_session` reads, with and without noise beside it.

    The spy counts rows returned, not SQL: `query_row`, `query_dicts` and
    `query_value` all go through `store.query`.
    """

    SID = "sess-alone"

    def rows_read(self):
        orig = store.query
        total = []

        def spy(sql, args=()):
            rows = orig(sql, args)
            total.append(len(rows))
            return rows

        store.query = spy
        try:
            api_session(self.SID)
        finally:
            store.query = orig
        return sum(total)

    def test_the_session_title_does_not_scan_the_other_sessions(self):
        seed_metric("claude_code.cost.usage", 0.1, self.SID)
        seed_log_event(
            "user_prompt", self.SID, [_attr("prompt", _str("the one asked for"))]
        )
        alone = self.rows_read()

        for n in range(30):
            for i in range(10):
                seed_log_event(
                    "user_prompt",
                    "noise-%d" % n,
                    [_attr("prompt", _str("noise %d" % i))],
                )
        self.assertEqual(self.rows_read(), alone)


class TestBothSessionEndpointsShareTheSameTotals(BaseDBTest):
    """The eight aggregated totals, seeded once and read on both endpoints.

    Asserting the two endpoints equal to each other would let a wrong column
    pass on both sides at once, so each is compared to the seeded figures.
    """

    SID = "sess-totals"
    TOTALS = {
        "cost": 0.42,
        "input_tokens": 1000,
        "cache_read_tokens": 500,
        "cache_creation_tokens": 200,
        "output_tokens": 300,
        "lines_added": 12,
        "lines_removed": 3,
        "active_seconds": 90,
    }

    def setUp(self):
        super().setUp()
        seed_metric("claude_code.cost.usage", 0.42, self.SID)
        seed_metric("claude_code.token.usage", 1000, self.SID, attr_type="input")
        seed_metric("claude_code.token.usage", 500, self.SID, attr_type="cacheRead")
        seed_metric("claude_code.token.usage", 200, self.SID, attr_type="cacheCreation")
        seed_metric("claude_code.token.usage", 300, self.SID, attr_type="output")
        seed_metric("claude_code.lines_of_code.count", 12, self.SID, attr_type="added")
        seed_metric("claude_code.lines_of_code.count", 3, self.SID, attr_type="removed")
        seed_metric("claude_code.active_time.total", 90, self.SID)

    def test_both_session_endpoints_carry_the_eight_seeded_totals(self):
        listed = api_sessions(NO_FILTER)["sessions"][0]
        head = api_session(self.SID)["head"]
        for key, value in self.TOTALS.items():
            self.assertAlmostEqual(listed[key], value, msg=key)
            self.assertAlmostEqual(head[key], value, msg=key)


class TestApiCostsOrigins(BaseDBTest):
    """api_costs: spend broken down by query_source. The fixture below is the
    only one in the suite seeding two distinct sources, which is what the
    ordering assertion needs -- TestApiWithSeedData seeds one."""

    SID = "session-req"

    def setUp(self):
        super().setUp()
        # repl_main_thread: 5 requests at $0.01; prompt_suggestion: one at $0.02.
        for dur in (100, 200, 300, 400, 500):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("model", _str("claude-opus-4-8")),
                    _attr("duration_ms", _int(dur)),
                    _attr("cost_usd", _dbl(0.01)),
                    _attr("input_tokens", _int(100)),
                    _attr("output_tokens", _int(50)),
                    _attr("query_source", _str("repl_main_thread")),
                ],
            )
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("model", _str("claude-sonnet-4-6")),
                _attr("duration_ms", _int(1000)),
                _attr("cost_usd", _dbl(0.02)),
                _attr("input_tokens", _int(10)),
                _attr("output_tokens", _int(5)),
                _attr("query_source", _str("prompt_suggestion")),
            ],
        )

    def test_origins_by_query_source(self):
        origins = {o["src"]: o for o in api_costs(NO_FILTER)["origins"]}
        self.assertEqual(origins["repl_main_thread"]["calls"], 5)
        self.assertAlmostEqual(origins["repl_main_thread"]["cost"], 0.05, places=4)
        self.assertEqual(origins["prompt_suggestion"]["calls"], 1)
        # sorted by cost desc
        self.assertEqual(api_costs(NO_FILTER)["origins"][0]["src"], "repl_main_thread")

    def test_each_origin_says_whether_it_is_the_main_thread(self):
        # The three spellings the main thread answers to, flagged by the API so
        # the page never has to know them.
        for source in ("main", "sdk"):
            seed_log_event(
                "api_request",
                self.SID,
                [_attr("cost_usd", _dbl(0.01)), _attr("query_source", _str(source))],
            )
        origins = {
            o["src"]: o["is_main_thread"] for o in api_costs(NO_FILTER)["origins"]
        }
        self.assertEqual(
            origins,
            {
                "repl_main_thread": True,
                "main": True,
                "sdk": True,
                "prompt_suggestion": False,
            },
        )

    def test_one_origin_is_one_row_whatever_the_style(self):
        # The five seeded main-thread calls plus these two are one origin: the
        # output style is not a seventh way of starting a request.
        for style in ("Concise", "Explanatory"):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("cost_usd", _dbl(0.01)),
                    _attr(
                        "query_source", _str("repl_main_thread:outputStyle:" + style)
                    ),
                ],
            )
        origins = {o["src"]: o for o in api_costs(NO_FILTER)["origins"]}
        self.assertEqual(sorted(origins), ["prompt_suggestion", "repl_main_thread"])
        self.assertEqual(origins["repl_main_thread"]["calls"], 7)


class TestApiContext(BaseDBTest):
    """api_context: sessions ranked by how hard their context pressed."""

    SID = "session-ctx"
    OTHER = "session-quiet"

    def _compaction(self, session, pre, trigger="auto"):
        attrs = [_attr("pre_tokens", _int(pre)), _attr("post_tokens", _int(1000))]
        if trigger is not None:
            attrs.append(_attr("trigger", _str(trigger)))
        seed_log_event("compaction", session, attrs)

    def _by_sid(self, f=None):
        return {s["session_id"]: s for s in api_context(f or NO_FILTER)["sessions"]}

    def test_an_auto_compaction_and_a_manual_one_are_not_added_up(self):
        # The whole view rests on this split: an auto compaction is the context
        # overflowing, a manual one is a decision. Summed together they say
        # nothing.
        self._compaction(self.SID, 90000, "auto")
        self._compaction(self.SID, 80000, "auto")
        self._compaction(self.SID, 70000, "manual")
        s = self._by_sid()[self.SID]
        self.assertEqual((s["auto_comp"], s["man_comp"]), (2, 1))

    def test_a_compaction_with_no_trigger_counts_as_manual_not_as_nothing(self):
        self._compaction(self.SID, 50000, trigger=None)
        s = self._by_sid()[self.SID]
        self.assertEqual((s["auto_comp"], s["man_comp"]), (0, 1))

    def test_the_peak_is_the_largest_context_seen_before_a_compaction(self):
        self._compaction(self.SID, 40000)
        self._compaction(self.SID, 120000)
        self._compaction(self.SID, 60000)
        self.assertEqual(self._by_sid()[self.SID]["pre_compaction_peak"], 120000)
        self.assertEqual(api_context(NO_FILTER)["pre_compaction_peak"], 120000)

    def test_the_max_context_is_the_highest_request_not_their_mean(self):
        # A context climbs monotonically, so a mean lands mid-ramp and answers
        # neither how high it went nor how long it stayed there.
        for read in (1000, 3000):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("model", _str("claude-opus-4-8")),
                    _attr("cache_read_tokens", _int(read)),
                    _attr("query_source", _str("repl_main_thread")),
                ],
            )
        # An event of another kind in the same session must not reach the column.
        seed_log_event("tool_result", self.SID, [_attr("tool_name", _str("Bash"))])
        self.assertEqual(self._by_sid()[self.SID]["max_context"], 3000)

    def test_the_max_context_sums_the_three_kinds_of_input_token(self):
        # Reading cache_read alone dropped the cache writes, which are the part
        # of a request that makes the context grow.
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("input_tokens", _int(40)),
                _attr("cache_read_tokens", _int(900)),
                _attr("cache_creation_tokens", _int(60)),
                _attr("query_source", _str("repl_main_thread")),
            ],
        )
        self.assertEqual(self._by_sid()[self.SID]["max_context"], 1000)

    def test_the_max_context_leaves_out_what_is_not_the_main_thread(self):
        # Same scope as the curve on a session detail: a sub-agent and a title
        # generation hold contexts of their own and are not this session's.
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("cache_read_tokens", _int(1000)),
                _attr("query_source", _str("repl_main_thread")),
            ],
        )
        for source, read in (
            ("agent:builtin:Explore", 90000),
            ("generate_session_title", 600),
        ):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("cache_read_tokens", _int(read)),
                    _attr("query_source", _str(source)),
                ],
            )
        self.assertEqual(self._by_sid()[self.SID]["max_context"], 1000)

    def test_the_max_context_reads_a_request_made_under_an_output_style(self):
        # The same scope question as the session curve, on the page that ranks
        # sessions: a styled main thread is still the main thread, and reading
        # the raw query_source here ranked those sessions at 0.
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("cache_read_tokens", _int(2500)),
                _attr("query_source", _str("repl_main_thread:outputStyle:Concise")),
            ],
        )
        self.assertEqual(self._by_sid()[self.SID]["max_context"], 2500)

    def test_a_session_holding_no_main_thread_request_falls_back_to_zero(self):
        # MAX over an empty CASE is NULL, and a NULL in the column makes the
        # table sort lexicographically.
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("cache_read_tokens", _int(5000)),
                _attr("query_source", _str("agent:builtin:general-purpose")),
            ],
        )
        self.assertEqual(self._by_sid()[self.SID]["max_context"], 0)

    def test_the_cost_column_sums_the_api_requests_of_the_session(self):
        for c in (0.25, 0.75):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("model", _str("claude-opus-4-8")),
                    _attr("cost_usd", _dbl(c)),
                ],
            )
        # A tool_result carries no cost and must not turn the sum into NULL.
        seed_log_event("tool_result", self.SID, [_attr("tool_name", _str("Bash"))])
        self.assertAlmostEqual(self._by_sid()[self.SID]["cost"], 1.0, places=6)

    def test_a_session_with_no_prompt_reports_no_ratio_rather_than_dividing(self):
        seed_log_event("tool_result", self.SID, [_attr("tool_name", _str("Bash"))])
        s = self._by_sid()[self.SID]
        self.assertEqual((s["prompts"], s["tools"], s["tools_per_prompt"]), (0, 1, 0))

    def test_tools_per_prompt_counts_both_kinds_of_event(self):
        seed_log_event("user_prompt", self.SID, [_attr("prompt_length", _int(10))])
        for _ in range(3):
            seed_log_event("tool_result", self.SID, [_attr("tool_name", _str("Bash"))])
        self.assertEqual(self._by_sid()[self.SID]["tools_per_prompt"], 3.0)

    def test_a_session_that_never_compacted_ships_zero_rather_than_null(self):
        # The table sorts numbers numerically and everything else as a string,
        # so a single None turns the ranking lexicographic.
        seed_log_event("tool_result", self.OTHER, [_attr("tool_name", _str("Bash"))])
        s = self._by_sid()[self.OTHER]
        self.assertEqual(
            (s["pre_compaction_peak"], s["max_context"], s["cost"]), (0, 0, 0)
        )
        for k in (
            "pre_compaction_peak",
            "max_context",
            "cost",
            "tools_per_prompt",
            "auto_comp",
            "man_comp",
            "events",
        ):
            self.assertIsInstance(s[k], (int, float), k)

    def test_the_heaviest_session_comes_first(self):
        self._compaction(self.OTHER, 10000, "manual")
        self._compaction(self.SID, 90000, "auto")
        self.assertEqual(api_context(NO_FILTER)["sessions"][0]["session_id"], self.SID)

    def test_the_project_filter_restricts_the_list(self):
        self._compaction(self.SID, 90000, "auto")
        seed_log_event(
            "compaction",
            self.OTHER,
            [
                _attr("trigger", _str("auto")),
                _attr("pre_tokens", _int(20000)),
                _attr("project", _str("other-project")),
            ],
        )
        f = replace(NO_FILTER, project="other-project")
        self.assertEqual(list(self._by_sid(f)), [self.OTHER])


class TestApiHooks(BaseDBTest):
    """api_health hooks: aggregation from the attrs blob, generic over hook_event."""

    SID = "session-hook"

    def setUp(self):
        super().setUp()
        # Two PreToolUse:Bash fires (40ms, 60ms, one with an error) + one
        # UserPromptSubmit, whose name carries no matcher.
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse:Bash")),
                _attr("hook_event", _str("PreToolUse")),
                _attr("total_duration_ms", _str("40")),
                _attr("num_non_blocking_error", _str("0")),
                _attr("num_blocking", _str("0")),
            ],
        )
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse:Bash")),
                _attr("hook_event", _str("PreToolUse")),
                _attr("total_duration_ms", _str("60")),
                _attr("num_non_blocking_error", _str("1")),
                _attr("num_blocking", _str("0")),
            ],
        )
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("UserPromptSubmit")),
                _attr("hook_event", _str("UserPromptSubmit")),
                _attr("total_duration_ms", _str("200")),
            ],
        )

    def test_hook_aggregates(self):
        bash = next(
            h for h in api_health(NO_FILTER)["hooks"] if h["name"] == "PreToolUse:Bash"
        )
        self.assertEqual(bash["fires"], 2)
        self.assertEqual(bash["avg"], 50.0)
        self.assertEqual(bash["max"], 60.0)
        self.assertEqual(bash["err"], 1)

    def test_another_hook_event_surfaces_generically(self):
        # Nothing is special-cased on PreToolUse: a hook fired on another event
        # gets its own card from the same grouping.
        post = next(
            h for h in api_health(NO_FILTER)["hooks"] if h["name"] == "UserPromptSubmit"
        )
        self.assertEqual(post["fires"], 1)
        self.assertEqual(post["event"], "UserPromptSubmit")

    def test_hook_detail_lists_fires(self):
        d = api_hook("PreToolUse:Bash")
        self.assertEqual(d["event"], "PreToolUse")
        self.assertEqual(len(d["fires"]), 2)
        self.assertEqual(d["fires"][0]["err"] + d["fires"][1]["err"], 1)

    def test_hook_detail_carries_origin(self):
        """Each fire names its session and project: a block is only actionable if
        you can tell which run it happened in."""
        fire = api_hook("UserPromptSubmit")["fires"][0]
        self.assertEqual(fire["session_id"], self.SID)
        self.assertEqual(fire["project"], "testproj")

    def test_rare_hook_is_not_evicted_by_a_frequent_one(self):
        """The HOOK_MAX_FIRES cap applies after the name filter. Applied before it,
        a hook firing once per prompt vanishes behind one firing on every tool
        call: an empty detail under a card counting hundreds."""
        for _ in range(HOOK_MAX_FIRES + 50):
            seed_log_event(
                "hook_execution_complete",
                self.SID,
                [
                    _attr("hook_name", _str("PreToolUse:Bash")),
                    _attr("hook_event", _str("PreToolUse")),
                    _attr("total_duration_ms", _str("5")),
                ],
            )
        self.assertEqual(len(api_hook("UserPromptSubmit")["fires"]), 1)
        self.assertEqual(len(api_hook("PreToolUse:Bash")["fires"]), HOOK_MAX_FIRES)

    def test_the_fire_cap_truncates_the_list_not_the_count(self):
        """A capped list read as a total is the failure mode: the card counts every
        fire, the detail lists the most recent HOOK_MAX_FIRES of them."""
        # setUp already seeded two PreToolUse:Bash fires.
        for _ in range(HOOK_MAX_FIRES - 1):
            seed_log_event(
                "hook_execution_complete",
                self.SID,
                [
                    _attr("hook_name", _str("PreToolUse:Bash")),
                    _attr("hook_event", _str("PreToolUse")),
                    _attr("total_duration_ms", _str("5")),
                ],
            )
        self.assertEqual(len(api_hook("PreToolUse:Bash")["fires"]), HOOK_MAX_FIRES)
        bash = next(
            h for h in api_health(NO_FILTER)["hooks"] if h["name"] == "PreToolUse:Bash"
        )
        self.assertEqual(bash["fires"], HOOK_MAX_FIRES + 1)

    def test_registrations_are_narrowed_to_the_matcher(self):
        """PreToolUse:Bash lists the Bash entries of the settings files, not
        every PreToolUse entry in them."""
        # The Write registration is synthetic: it takes two matchers to show the
        # narrowing, and this export only ever registered one.
        for matcher in ("Bash", "Write"):
            seed_log_event(
                "hook_registered",
                self.SID,
                [
                    _attr("hook_event", _str("PreToolUse")),
                    _attr("hook_type", _str("command")),
                    _attr("hook_source", _str("userSettings")),
                    _attr("hook_matcher", _str(matcher)),
                ],
            )
        regs = api_hook("PreToolUse:Bash")["regs"]
        self.assertEqual([r["matcher"] for r in regs], ["Bash"])

    def test_a_registration_seen_twice_is_listed_once(self):
        """The same entry is re-registered on every session start."""
        for _ in range(3):
            seed_log_event(
                "hook_registered",
                self.SID,
                [
                    _attr("hook_event", _str("PreToolUse")),
                    _attr("hook_type", _str("command")),
                    _attr("hook_source", _str("userSettings")),
                    _attr("hook_matcher", _str("Bash")),
                ],
            )
        self.assertEqual(len(api_hook("PreToolUse:Bash")["regs"]), 1)

    def test_a_fire_without_a_duration_still_counts(self):
        """A hook that exported no total_duration_ms is a fire like any other:
        it lands in the count, and it averages as zero rather than dropping out
        of the average."""
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("Stop")),
                _attr("hook_event", _str("Stop")),
            ],
        )
        stop = next(h for h in api_health(NO_FILTER)["hooks"] if h["name"] == "Stop")
        self.assertEqual(stop["fires"], 1)
        self.assertEqual(stop["avg"], 0)
        self.assertEqual(stop["max"], 0)
        self.assertEqual(stop["total_duration_ms"], 0)

    def test_a_nameless_fire_is_reachable_under_its_card(self):
        """hook_stats groups a fire carrying neither name nor event under '?';
        the detail answered nothing for that card."""
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("total_duration_ms", _str("15")),
            ],
        )
        self.assertIn("?", [h["name"] for h in api_health(NO_FILTER)["hooks"]])
        self.assertEqual(len(api_hook("?")["fires"]), 1)


class TestApiSubagents(BaseDBTest):
    """subagents_stats: real per-delegation metrics parsed from subagent_completed."""

    SID = "session-sub"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("general-purpose")),
                _attr("agent.source", _str("built-in")),
                _attr("is_built_in", _bool(True)),
                _attr("is_async", _bool(True)),
                _attr("model", _str("claude-sonnet-4-6")),
                _attr("total_tokens", _int(1200)),
                _attr("total_tool_uses", _int(7)),
                _attr("duration_ms", _int(4500)),
                _attr("prompt.id", _str("p-sub")),
            ],
        )
        # The spawning Agent call carries the instructions, linked by prompt.id.
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Agent")),
                _attr("prompt.id", _str("p-sub")),
                _attr("success", _str("true")),
                _attr(
                    "tool_parameters",
                    _str(json.dumps({"subagent_type": "general-purpose"})),
                ),
                _attr(
                    "tool_input",
                    _str(
                        json.dumps(
                            {
                                "description": "Do X",
                                "prompt": "Detailed instructions here",
                                "subagent_type": "general-purpose",
                                "isolation": "worktree",
                                "run_in_background": True,
                            }
                        )
                    ),
                ),
            ],
        )

    def test_analysis_includes_subagents(self):
        subs = api_analysis(NO_FILTER)["subagents"]
        self.assertEqual(len(subs), 1)
        s = subs[0]
        self.assertEqual(s["agent_type"], "general-purpose")
        self.assertEqual(s["model"], "Sonnet")
        self.assertEqual(s["tokens"], 1200)
        self.assertEqual(s["tools"], 7)
        self.assertEqual(s["duration_ms"], 4500.0)
        self.assertEqual(s["session_id"], self.SID)
        self.assertEqual(s["project"], "testproj")

    def test_subagent_detail_joins_instructions(self):
        eid = store.query_row("SELECT id FROM events WHERE name='subagent_completed'")[
            "id"
        ]
        d = api_subagent(eid)
        self.assertEqual(d["agent_type"], "general-purpose")
        self.assertEqual(d["tokens"], 1200)
        self.assertEqual(d["tools"], 7)
        self.assertEqual(d["description"], "Do X")
        self.assertEqual(d["instructions"], "Detailed instructions here")
        self.assertEqual(d["isolation"], "worktree")

    def test_subagent_detail_missing_returns_error(self):
        self.assertIn("error", api_subagent(999999))

    def test_subagent_detail_lists_the_efforts_of_its_own_requests(self):
        # The completion event carries no effort; the api_requests the agent made
        # do, keyed by prompt and agent origin. A built-in agent answers to
        # `agent:builtin:<type>`, so the main thread's requests are not its own.
        for origin, effort in (
            ("agent:builtin:general-purpose", "xhigh"),
            ("agent:builtin:general-purpose", "high"),
            ("agent:builtin:general-purpose", "xhigh"),
            ("repl_main_thread", "medium"),
        ):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("query_source", _str(origin)),
                    _attr("effort", _str(effort)),
                    _attr("prompt.id", _str("p-sub")),
                ],
            )
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("query_source", _str("agent:builtin:general-purpose")),
                _attr("effort", _str("low")),
                _attr("prompt.id", _str("p-other")),
            ],
        )
        eid = store.query_row("SELECT id FROM events WHERE name='subagent_completed'")[
            "id"
        ]
        self.assertEqual(api_subagent(eid)["efforts"], ["high", "xhigh"])

    def test_a_custom_subagent_reads_the_flat_agent_custom_origin(self):
        # Every user-defined agent emits `agent:custom`, whatever its name.
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("dev-review")),
                _attr("is_built_in", _bool(False)),
                _attr("prompt.id", _str("p-custom")),
            ],
        )
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("query_source", _str("agent:custom")),
                _attr("effort", _str("medium")),
                _attr("prompt.id", _str("p-custom")),
            ],
        )
        eid = store.query_row(
            "SELECT id FROM events WHERE name='subagent_completed' "
            "AND agent_type='dev-review'"
        )["id"]
        self.assertEqual(api_subagent(eid)["efforts"], ["medium"])

    def test_a_subagent_without_a_prompt_lists_no_effort(self):
        # Rows ingested before prompt.id existed cannot be joined to anything.
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("Explore")),
            ],
        )
        eid = store.query_row(
            "SELECT id FROM events WHERE name='subagent_completed' "
            "AND agent_type='Explore'"
        )["id"]
        self.assertEqual(api_subagent(eid)["efforts"], [])


class TestApiPrompt(BaseDBTest):
    """api_prompt: everything one turn set off, scoped on prompt_id."""

    SID = "session-prompt"
    PID = "p-detail"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "user_prompt",
            self.SID,
            [
                _attr("prompt.id", _str(self.PID)),
                _attr("prompt", _str("Refactor the billing service")),
            ],
        )
        for cost, tout in ((0.5, 100), (1.5, 400)):
            seed_log_event(
                "api_request",
                self.SID,
                [
                    _attr("prompt.id", _str(self.PID)),
                    _attr("model", _str("claude-opus-4-8")),
                    _attr("cost_usd", _dbl(cost)),
                    _attr("input_tokens", _int(10)),
                    _attr("output_tokens", _int(tout)),
                    _attr("cache_read_tokens", _int(9000)),
                    _attr("cache_creation_tokens", _int(200)),
                ],
            )
        for name, size, ok in (
            ("Bash", 5000, "true"),
            ("Bash", 300, "false"),
            ("Read", 800, "true"),
        ):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("prompt.id", _str(self.PID)),
                    _attr("tool_name", _str(name)),
                    _attr("success", _str(ok)),
                    _attr("tool_result_size_bytes", _str(str(size))),
                ],
            )
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("prompt.id", _str(self.PID)),
                _attr("hook_name", _str("PreToolUse:Bash")),
                _attr("total_duration_ms", _str("40")),
            ],
        )
        # A neighbouring turn in the same session: nothing of it may leak in.
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("prompt.id", _str("p-other")),
                _attr("tool_name", _str("Grep")),
                _attr("tool_result_size_bytes", _str("70000")),
            ],
        )

    def test_totals_are_scoped_to_the_prompt(self):
        d = api_prompt(self.PID)
        self.assertEqual(d["calls"], 2)
        self.assertEqual(d["cost"], 2.0)
        self.assertEqual(d["hook_ms"], 40.0)

    def test_tokens_split_by_type(self):
        d = api_prompt(self.PID)
        self.assertEqual((d["input_tokens"], d["output_tokens"]), (20, 500))
        self.assertEqual(
            (d["cache_read_tokens"], d["cache_creation_tokens"]), (18000, 400)
        )

    def test_head_carries_text_session_and_project(self):
        d = api_prompt(self.PID)
        self.assertEqual(d["prompt_text"], "Refactor the billing service")
        self.assertEqual(d["session_id"], self.SID)
        self.assertEqual(d["project"], "testproj")

    def test_neighbouring_turn_does_not_leak(self):
        """Every event of a session shares its session_id but not its prompt_id:
        the 70 KB Grep of the next turn must stay out of this one."""
        labels = [t["label"] for t in api_prompt(self.PID)["toolstats"]]
        self.assertEqual(sorted(labels), ["Bash", "Read"])

    def test_unknown_prompt_returns_error(self):
        self.assertIn("error", api_prompt("no-such-prompt"))

    def test_turn_without_its_user_prompt_event(self):
        """Logs enabled mid-session, or prompt content off: the turn has tool and
        api_request events but no user_prompt. It must still resolve, from the span."""
        d = api_prompt("p-other")
        self.assertNotIn("error", d)
        self.assertIsNone(d["prompt_text"])
        self.assertEqual(d["session_id"], self.SID)
        self.assertEqual([t["label"] for t in d["toolstats"]], ["Grep"])

    def test_calls_can_be_scoped_to_a_prompt(self):
        """The tools table drills down through api_calls, widened with `prompt`."""
        scoped = api_calls("Bash", NO_FILTER, None, self.PID)
        self.assertEqual(len(scoped), 2)
        self.assertEqual(len(api_calls("Grep", NO_FILTER, None, self.PID)), 0)
        self.assertEqual(len(api_calls("Grep", NO_FILTER)), 1)


class TestApiSubagentToolInput(BaseDBTest):
    """Malformed shapes of `tool_input` on the Agent event that carries the
    instructions. The key arrives sometimes as a dict, sometimes as a JSON
    string, sometimes unreadable: the join must degrade without raising."""

    SID = "session-sub-ti"

    def _seed(self, tool_input_attr, tool_name="Agent", prompt_id="p-ti"):
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("Explore")),
                _attr("total_tokens", _int(10)),
                _attr("prompt.id", _str(prompt_id)),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str(tool_name)),
                _attr("prompt.id", _str(prompt_id)),
            ]
            + ([tool_input_attr] if tool_input_attr else []),
        )
        return store.query_value(
            "SELECT id FROM events WHERE name='subagent_completed' AND prompt_id=?",
            (prompt_id,),
        )

    def test_tool_input_as_dict(self):
        # Defensive: this export only ever sends the JSON-string form, but the
        # decoder turns a kvlistValue into a dict and `_tool_input` takes it.
        eid = self._seed(
            _attr(
                "tool_input",
                {
                    "kvlistValue": {
                        "values": [
                            _attr("description", _str("dict form")),
                            _attr("prompt", _str("instr")),
                        ]
                    }
                },
            )
        )
        d = api_subagent(eid)
        self.assertEqual(d["description"], "dict form")
        self.assertEqual(d["instructions"], "instr")

    def test_a_task_spawn_is_still_joined(self):
        # Defensive: the tool was renamed Task -> Agent, and the query still
        # accepts both. A database holding older sessions depends on it.
        eid = self._seed(
            _attr("tool_input", _str(json.dumps({"description": "legacy"}))),
            tool_name="Task",
        )
        self.assertEqual(api_subagent(eid)["description"], "legacy")

    def test_an_unreadable_tool_input_leaves_the_metrics_standing(self):
        """Every shape that yields no usable dict: a parse failure, a scalar, an
        array, an absent key, and an empty object -- which is a valid dict, so it
        passes the filter and clears the fields without breaking out of the
        search. None of them may cost the sub-agent its own metrics."""
        shapes = (
            ("invalid json", _attr("tool_input", _str("{not json at all"))),
            ("not an object", _attr("tool_input", _str("plain string"))),
            ("json array", _attr("tool_input", _str("[1, 2, 3]"))),
            ("absent", None),
            ("empty object", _attr("tool_input", _str("{}"))),
        )
        for i, (shape, attribute) in enumerate(shapes):
            with self.subTest(shape=shape):
                d = api_subagent(self._seed(attribute, prompt_id="p-ti-%d" % i))
                self.assertIsNone(d["description"])
                self.assertIsNone(d["instructions"])
                self.assertIsNone(d["isolation"])
                self.assertEqual(d["agent_type"], "Explore")
                self.assertEqual(d["tokens"], 10)

    def test_background_persists_across_candidates(self):
        """Two Agent calls for the same prompt, neither matching the type: the
        fields of the last one read win, but `background` is only overwritten by
        an explicit run_in_background, so it survives the next one."""
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("Explore")),
                _attr("prompt.id", _str("p-bg")),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Agent")),
                _attr("prompt.id", _str("p-bg")),
                _attr(
                    "tool_input",
                    _str(
                        json.dumps(
                            {
                                "description": "premier",
                                "run_in_background": True,
                                "subagent_type": "autre",
                            }
                        )
                    ),
                ),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Agent")),
                _attr("prompt.id", _str("p-bg")),
                _attr(
                    "tool_input",
                    _str(
                        json.dumps(
                            {
                                "description": "second",
                                "subagent_type": "encore-autre",
                            }
                        )
                    ),
                ),
            ],
        )
        eid = store.query_row("SELECT id FROM events WHERE name='subagent_completed'")[
            "id"
        ]
        d = api_subagent(eid)
        self.assertEqual(d["description"], "second")
        self.assertTrue(d["background"])

    def test_matching_subagent_type_wins(self):
        """The Agent call whose subagent_type matches stops the search, even if
        another one of the same prompt follows."""
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("Explore")),
                _attr("prompt.id", _str("p-match")),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Agent")),
                _attr("prompt.id", _str("p-match")),
                _attr(
                    "tool_input",
                    _str(
                        json.dumps(
                            {
                                "description": "le bon",
                                "subagent_type": "Explore",
                            }
                        )
                    ),
                ),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Agent")),
                _attr("prompt.id", _str("p-match")),
                _attr("tool_input", _str(json.dumps({"description": "le suivant"}))),
            ],
        )
        eid = store.query_row("SELECT id FROM events WHERE name='subagent_completed'")[
            "id"
        ]
        self.assertEqual(api_subagent(eid)["description"], "le bon")


class TestApiBashCalls(BaseDBTest):
    """bash_calls: per-invocation rows with the description from the params column."""

    SID = "session-bash"

    def setUp(self):
        super().setUp()
        for i, (cmd, desc) in enumerate(
            (
                ("ls -la", "List files"),
                ("git status", "Check git status"),
            )
        ):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("tool_name", _str("Bash")),
                    _attr("success", _str("true")),
                    _attr("duration_ms", _str(str(100 + i))),
                    _attr("tool_result_size_bytes", _str("200")),
                    _attr(
                        "tool_parameters",
                        _str(
                            json.dumps(
                                {
                                    "bash_command": cmd.split()[0],
                                    "full_command": cmd,
                                    "description": desc,
                                }
                            )
                        ),
                    ),
                ],
            )

    def test_one_row_per_call_with_its_description_command_and_origin(self):
        # The description and the full command are read out of the params
        # column, and the global table spans projects and sessions: each row
        # says where it comes from.
        calls = api_analysis(NO_FILTER)["bash"]
        self.assertEqual(len(calls), 2)
        self.assertEqual({c["desc"] for c in calls}, {"List files", "Check git status"})
        ls = next(c for c in calls if c["desc"] == "List files")
        self.assertIn("id", ls)
        self.assertEqual(ls["cmd"], "ls -la")
        self.assertEqual(ls["session_id"], self.SID)
        self.assertEqual(ls["project"], "testproj")


class TestApiErrorsCalls(BaseDBTest):
    """errors_calls: per-invocation failures with the message from the attrs blob."""

    SID = "session-err"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("false")),
                _attr("error_type", _str("ShellError")),
                _attr("error", _str("Shell command failed")),
                _attr("duration_ms", _str("30")),
                _attr(
                    "tool_parameters",
                    _str(
                        '{"full_command":"grep -rn missing app/","bash_command":"grep"}'
                    ),
                ),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Edit")),
                _attr("success", _str("true")),
            ],
        )

    def test_a_failure_is_one_row_carrying_its_message_command_and_origin(self):
        """Only the failing calls, one row each. The message is the same on every
        shell failure; the command is what tells two rows apart, so the list has
        to carry it, along with the session and project it came from."""
        errs = api_analysis(NO_FILTER)["errors"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error_type"], "ShellError")
        self.assertEqual(errs[0]["msg"], "Shell command failed")
        self.assertIn("id", errs[0])
        self.assertEqual(errs[0]["cmd"], "grep -rn missing app/")
        self.assertEqual(errs[0]["session_id"], self.SID)
        self.assertEqual(errs[0]["project"], "testproj")

    # The nine sections the five analysis tabs read, plus the cut flag. Spelled
    # out here rather than read back off the code: a section silently dropped
    # from the payload is what a test built from the payload would not see.
    SECTIONS = {
        "tools",
        "bash",
        "skills",
        "mcp",
        "decisions",
        "errors",
        "api_errors",
        "prompts",
        "subagents",
        "truncated",
    }

    def test_the_payload_carries_every_section(self):
        self.assertEqual(set(api_analysis(NO_FILTER)), self.SECTIONS)

    def test_the_route_narrows_nothing(self):
        """The route takes no parameter of its own: a query string asking for a
        subset gets the whole payload, as any other query string does."""
        handler = api.API_ROUTES["/api/analysis"]
        payload = handler({"only": ["bash"], "limit": ["5"]}, NO_FILTER)
        self.assertEqual(set(payload), self.SECTIONS)

    def test_a_cut_list_says_so(self):
        """The aggregates cover the whole window whatever its length, so a list
        that stopped at its ceiling has to name itself or it reads as complete."""
        for _ in range(ANALYSIS_CAPS["errors"] + 1):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("tool_name", _str("Bash")),
                    _attr("success", _str("false")),
                    _attr("error_type", _str("ShellError")),
                ],
            )
        d = api_analysis(NO_FILTER)
        self.assertEqual(len(d["errors"]), ANALYSIS_CAPS["errors"])
        self.assertEqual(d["truncated"], ["errors"])

    def test_a_list_that_merely_fills_its_ceiling_is_not_cut(self):
        """The n+1 probe is the whole point: exactly `cap` rows is complete."""
        # setUp already seeded one failure, hence the -1.
        for _ in range(ANALYSIS_CAPS["errors"] - 1):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("tool_name", _str("Bash")),
                    _attr("success", _str("false")),
                    _attr("error_type", _str("ShellError")),
                ],
            )
        d = api_analysis(NO_FILTER)
        self.assertEqual(len(d["errors"]), ANALYSIS_CAPS["errors"])
        self.assertEqual(d["truncated"], [])

    def test_command_is_none_when_the_tool_is_not_bash(self):
        seed_log_event(
            "tool_result",
            "session-err2",
            [
                _attr("tool_name", _str("Read")),
                _attr("success", _str("false")),
                _attr("error_type", _str("FileTooLargeError")),
                _attr("error", _str("File content exceeds maximum allowed tokens")),
            ],
        )
        errs = api_analysis(NO_FILTER)["errors"]
        read = [e for e in errs if e["tool_name"] == "Read"]
        self.assertEqual(len(read), 1)
        self.assertIsNone(read[0]["cmd"])


class TestTheTimelineHidesTheHookStartRow(BaseDBTest):
    """A hook fire is exported twice: `hook_execution_start` and, once it is
    done, `hook_execution_complete` carrying every attribute the first one had
    plus its result and its duration. Two timeline rows for one fire, the first
    of them empty. The start row is dropped at read time only -- it stays
    stored, and Diagnostics keeps counting it.
    """

    SID = "session-hooks"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "hook_execution_start",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse:Bash")),
            ],
        )
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse:Bash")),
                _attr("total_duration_ms", _str("52")),
            ],
        )
        seed_log_event("user_prompt", self.SID, [_attr("prompt", _str("go"))])

    def test_the_timeline_keeps_the_complete_row_alone(self):
        names = [e["name"] for e in api_session(self.SID)["events"]]
        self.assertIn("hook_execution_complete", names)
        self.assertIn("user_prompt", names)
        self.assertNotIn("hook_execution_start", names)

    def test_diagnostics_still_counts_it(self):
        counts = {r["name"]: r["points"] for r in api_health(NO_FILTER)["event_names"]}
        self.assertEqual(counts["hook_execution_start"], 1)


class TestTheSessionPanelKnowsWhereItRan(BaseDBTest):
    """`terminal.type` and `service.version` ride on nearly every record and
    were never read. Both are lists rather than scalars: a session can change
    terminal, and one long enough sees the CLI update under it."""

    SID = "session-where"
    OTHER = "session-elsewhere"

    def test_the_terminals_are_deduplicated(self):
        """Also the test of the quoting: `$.terminal.type` reads a nested path
        and would return nothing at all on every row."""
        for terminal in ("ghostty", "ghostty", "phpstorm"):
            seed_log_event(
                "api_request", self.SID, [_attr("terminal.type", _str(terminal))]
            )
        self.assertEqual(
            api_session(self.SID)["head"]["terminals"], ["ghostty", "phpstorm"]
        )

    def test_the_attribute_is_read_off_every_kind_of_record(self):
        """Neither attribute is on every record, and the misses are mostly
        api_request rows -- narrowing to that name, as the `efforts` query
        beside it does, would lose sessions that carry it elsewhere."""
        seed_log_event(
            "user_prompt", self.SID, [_attr("terminal.type", _str("ssh-session"))]
        )
        self.assertEqual(api_session(self.SID)["head"]["terminals"], ["ssh-session"])

    def test_a_session_carrying_neither_ships_two_empty_lists(self):
        seed_log_event("user_prompt", self.SID, [_attr("prompt", _str("go"))])
        head = api_session(self.SID)["head"]
        self.assertEqual(head["terminals"], [])
        self.assertEqual(head["versions"], [])

    def test_the_versions_come_in_the_order_they_appeared(self):
        """Not the alphabetical order: 2.1.76 sorts after 2.1.237 as text, and a
        session that updated mid-run would read as having gone backwards."""
        for version in ("2.1.76", "2.1.161"):
            seed_log_event(
                "api_request", self.SID, [_attr("service.version", _str(version))]
            )
        self.assertEqual(
            api_session(self.SID)["head"]["versions"], ["2.1.76", "2.1.161"]
        )

    def test_another_session_does_not_leak_in(self):
        seed_log_event(
            "api_request", self.SID, [_attr("terminal.type", _str("ghostty"))]
        )
        seed_log_event(
            "api_request", self.OTHER, [_attr("terminal.type", _str("vscode"))]
        )
        self.assertEqual(api_session(self.SID)["head"]["terminals"], ["ghostty"])


class TestTheTimelineShipsWhatItSummarises(BaseDBTest):
    """The timeline query reads its summaries out of the attrs blob, so the
    types SQLite gives back are the ones the browser gets."""

    SID = "session-detail"

    def test_a_hook_duration_comes_back_as_a_number(self):
        """Claude Code exports `total_duration_ms` as text on every version seen.
        Without the CAST the browser compares it to its highlight threshold as a
        string, where "52" is above "3791"."""
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("UserPromptSubmit")),
                _attr("total_duration_ms", _str("484")),
            ],
        )
        row = api_session(self.SID)["events"][0]
        self.assertEqual(row["hook_ms"], 484.0)
        self.assertNotIsInstance(row["hook_ms"], str)


class TestApiProviderErrors(BaseDBTest):
    """provider_errors: one row per incident, not per event.

    Claude Code emits an `api_retries_exhausted` beside the `api_error` it
    closes -- same timestamp, same session, same prompt.id -- so counting events
    would count 27 incidents where there are 16. The pair is folded onto the
    api_error row; an api_retries_exhausted no api_error faces still ships on
    its own, so the folding can only ever enrich a row, never hide one.
    """

    SID1 = "session-apierr"
    SID2 = "session-apierr-other"
    T = 1_700_000_000

    def setUp(self):
        super().setUp()
        # Paired incident: the error and the exhausted retries of the same call.
        seed_log_event(
            "api_error",
            self.SID1,
            [
                _attr("status_code", _int(529)),
                _attr("error", _str("Overloaded")),
                _attr("attempt", _int(11)),
                _attr("duration_ms", _str("195118")),
                _attr("model", _str("claude-opus-5")),
                _attr("prompt.id", _str("P1")),
            ],
            ts=self.T,
        )
        seed_log_event(
            "api_retries_exhausted",
            self.SID1,
            [
                # A string on purpose: the CLI ships these two as text.
                _attr("total_attempts", _str("11")),
                _attr("total_retry_duration_ms", _str("195164")),
                _attr("model", _str("claude-opus-5")),
                _attr("prompt.id", _str("P1")),
            ],
            ts=self.T,
        )
        # An error that never exhausted its retries.
        seed_log_event(
            "api_error",
            self.SID1,
            [
                _attr("status_code", _int(429)),
                _attr("error", _str("Rate limited")),
                _attr("attempt", _int(1)),
                _attr("duration_ms", _str("1200")),
                _attr("prompt.id", _str("P2")),
            ],
            ts=self.T - 100,
        )
        # An orphan: exhausted retries with no api_error facing it.
        seed_log_event(
            "api_retries_exhausted",
            self.SID1,
            [
                _attr("total_attempts", _str("4")),
                _attr("total_retry_duration_ms", _str("9000")),
                _attr("prompt.id", _str("P3")),
            ],
            ts=self.T - 200,
        )
        # Another session, to pin the scope of api_session.
        seed_log_event(
            "api_error",
            self.SID2,
            [
                _attr("status_code", _int(500)),
                _attr("error", _str("Internal")),
                _attr("attempt", _int(2)),
                _attr("duration_ms", _str("800")),
                _attr("prompt.id", _str("P4")),
            ],
            ts=self.T - 300,
        )

    def _rows(self):
        return api_analysis(NO_FILTER)["api_errors"]

    def test_a_pair_is_one_row(self):
        rows = self._rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            [r["ts"] for r in rows], sorted((r["ts"] for r in rows), reverse=True)
        )

    def test_the_paired_row_is_the_error_carrying_the_retry_totals(self):
        """The cumulative retry duration wins over the last attempt's own: the
        incident lasted the whole retry chain, not its final call."""
        row = next(r for r in self._rows() if r["error"] == "Overloaded")
        stored_id = store.query_value(
            "SELECT id FROM events WHERE name='api_error' AND prompt_id='P1'"
        )
        self.assertEqual(row["id"], stored_id)
        self.assertEqual(row["name"], "api_error")
        self.assertEqual(row["attempts"], 11)
        self.assertEqual(row["duration_ms"], 195164.0)
        self.assertTrue(row["exhausted"])
        self.assertEqual(row["model"], "Opus")

    def test_a_lone_error_keeps_its_own_figures(self):
        row = next(r for r in self._rows() if r["status_code"] == 429)
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["duration_ms"], 1200)
        self.assertFalse(row["exhausted"])

    def test_an_orphan_exhaustion_is_never_lost(self):
        """The row the folding could have swallowed: nothing pairs with it, and
        it still ships."""
        row = next(r for r in self._rows() if r["name"] == "api_retries_exhausted")
        self.assertEqual(row["attempts"], 4)
        self.assertEqual(row["duration_ms"], 9000.0)
        self.assertTrue(row["exhausted"])

    def test_the_retry_duration_comes_back_as_a_number(self):
        """`total_retry_duration_ms` comes out of the blob as text and rides
        straight into the payload -- nothing recasts it Python-side, unlike
        `attempts`, which `as_int` normalises whatever the SQL did. Without the
        CAST the front would sort the Duration column as strings."""
        for row in self._rows():
            with self.subTest(id=row["id"]):
                self.assertIsInstance(row["duration_ms"], (int, float))

    def test_the_session_scope_keeps_its_own_rows(self):
        rows = api_session(self.SID1)["api_errors"]
        self.assertEqual(len(rows), 3)
        other = api_session(self.SID2)["api_errors"]
        self.assertEqual(len(other), 1)
        self.assertNotIn(other[0]["id"], [r["id"] for r in rows])

    def test_the_global_scope_mixes_the_sessions(self):
        """The window payload is not a session detail: the rows of both sessions
        belong to the same list, and the folding runs across them."""
        self.assertEqual(
            {r["session_id"] for r in self._rows()}, {self.SID1, self.SID2}
        )


class TestTheIncidentPairingHoldsAtItsEdges(BaseDBTest):
    """What the pairing does when the base stops looking like it does today.

    `ts` is an epoch *second*: nano_to_s truncates. The two records of one
    incident are emitted milliseconds apart, so they land in the same second
    only most of the time -- and `prompt.id` is an attribute a CLI version is
    free to stop exporting. None of these shapes exists in the base measured
    for this work, and each of them is a way to render an incident twice, or
    twice with the same id.
    """

    SID = "session-pairing"
    T = 1_700_000_000

    def _error(self, prompt_id, ts, status=529, attempt=3, duration="1000"):
        attrs = [
            _attr("status_code", _int(status)),
            _attr("error", _str("Overloaded")),
            _attr("attempt", _int(attempt)),
            _attr("duration_ms", _str(duration)),
        ]
        if prompt_id:
            attrs.append(_attr("prompt.id", _str(prompt_id)))
        seed_log_event("api_error", self.SID, attrs, ts=ts)

    def _exhausted(self, prompt_id, ts, attempts="7", retry_ms="70000"):
        attrs = [
            _attr("total_attempts", _str(attempts)),
            _attr("total_retry_duration_ms", _str(retry_ms)),
        ]
        if prompt_id:
            attrs.append(_attr("prompt.id", _str(prompt_id)))
        seed_log_event("api_retries_exhausted", self.SID, attrs, ts=ts)

    def _rows(self):
        return api_analysis(NO_FILTER)["api_errors"]

    def test_a_pair_straddling_a_second_is_still_one_incident(self):
        """The error at ...000.999 and its exhaustion at ...001.001 store one
        second apart. Pairing on equality left the incident as two rows, the
        second of them claiming no error was reported -- with the error sitting
        right beside it in the table."""
        self._error("P1", self.T)
        self._exhausted("P1", self.T + 1)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "api_error")
        self.assertTrue(rows[0]["exhausted"])
        self.assertEqual(rows[0]["attempts"], 7)
        self.assertEqual(rows[0]["duration_ms"], 70000.0)

    def test_a_pair_two_seconds_apart_is_two_incidents(self):
        """The window is one second and no wider: past it the two records are
        read as what they say they are rather than guessed to be one call."""
        self._error("P1", self.T)
        self._exhausted("P1", self.T + 2)
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["name"] for r in rows}, {"api_error", "api_retries_exhausted"}
        )

    def test_two_exhaustions_on_one_error_never_share_a_row_id(self):
        """The fan-out of the join: folding both onto the same error would ship
        two rows carrying the same `id`, which the front uses as the row key and
        as what the modal fetches. One of them lends its totals, the other ships
        as a row of its own."""
        self._error("P1", self.T)
        self._exhausted("P1", self.T, attempts="7", retry_ms="70000")
        self._exhausted("P1", self.T, attempts="9", retry_ms="90000")
        rows = self._rows()
        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "two rows share an id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(r["name"] for r in rows), ["api_error", "api_retries_exhausted"]
        )

    def test_an_incident_with_no_prompt_id_is_still_one_row(self):
        """`json_extract(...) = json_extract(...)` is NULL when both sides are:
        left unguarded the join misses *and* the orphan clause matches, which
        renders an incident whose records carry no prompt.id twice, the error
        marked as having not exhausted its retries."""
        self._error(None, self.T)
        self._exhausted(None, self.T)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "api_error")
        self.assertTrue(rows[0]["exhausted"])
        self.assertEqual(rows[0]["attempts"], 7)

    def test_one_exhaustion_stamps_one_error_only(self):
        """Two errors of the same key against one exhaustion: handing both the
        totals of the chain would count its duration twice in the table and
        leave neither error with its own figures."""
        self._error("P1", self.T, status=529, attempt=3, duration="1000")
        self._error("P1", self.T, status=529, attempt=5, duration="2000")
        self._exhausted("P1", self.T, attempts="7", retry_ms="70000")
        rows = self._rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["exhausted"] for r in rows].count(True), 1)
        stamped = next(r for r in rows if r["exhausted"])
        self.assertEqual(stamped["duration_ms"], 70000.0)
        own = next(r for r in rows if not r["exhausted"])
        self.assertIn(own["duration_ms"], (1000, 2000))
        self.assertIn(own["attempts"], (3, 5))


class TestApiDecisions(BaseDBTest):
    """decisions_stats: the permission verdicts, grouped by tool, decision and
    source. tool_decision is the event Claude Code sends most -- one before every
    tool call -- and the only one feeding the `decision` column."""

    SID = "session-dec"
    OTHER = "session-dec-other"

    def _decide(self, tool, decision, source, session=None):
        seed_log_event(
            "tool_decision",
            session or self.SID,
            [
                _attr("decision", _str(decision)),
                _attr("source", _str(source)),
                _attr("tool_name", _str(tool)),
                _attr("tool_use_id", _str("toolu-%s-%s" % (tool, decision))),
            ],
        )

    def setUp(self):
        super().setUp()
        for _ in range(3):
            self._decide("Bash", "accept", "config")
        self._decide("Bash", "reject", "user_reject")
        for _ in range(2):
            self._decide("Edit", "accept", "user_temporary")
        self._decide("Read", "accept", "config", session=self.OTHER)
        # The tool call that followed the accepted decision: it carries
        # decision_source too, and must not be counted as a decision.
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("true")),
                _attr("decision_source", _str("config")),
                _attr("decision_type", _str("accept")),
            ],
        )

    def test_grouped_by_tool_decision_and_source(self):
        groups = {
            (d["tool_name"], d["decision"], d["dec_source"]): d["decisions"]
            for d in api_analysis(NO_FILTER)["decisions"]
        }
        self.assertEqual(
            groups,
            {
                ("Bash", "accept", "config"): 3,
                ("Bash", "reject", "user_reject"): 1,
                ("Edit", "accept", "user_temporary"): 2,
                ("Read", "accept", "config"): 1,
            },
        )

    def test_the_busiest_group_comes_first(self):
        self.assertEqual(api_analysis(NO_FILTER)["decisions"][0]["decisions"], 3)

    def test_a_tool_result_is_not_a_decision(self):
        # It carries decision_source and decision_type, which look like the same
        # thing; counting it would double every accepted call.
        self.assertEqual(
            sum(d["decisions"] for d in api_analysis(NO_FILTER)["decisions"]), 7
        )

    def test_the_session_detail_only_counts_its_own(self):
        groups = api_session(self.SID)["decisions"]
        self.assertEqual(sum(d["decisions"] for d in groups), 6)
        self.assertNotIn("Read", [d["tool_name"] for d in groups])


class TestTheCapsAreTheNumbersTheyClaim(BaseDBTest):
    """The four list ceilings asserted against their figures, not against the
    constants that hold them. `assertEqual(len(rows), ANALYSIS_CAPS["bash"])` stays
    green when that 300 becomes a 3: the assertion follows the mutation, and a cap
    quietly dropped to a handful is exactly what a reader of a full-looking list
    would not see. Each list is seeded one row past its ceiling.
    """

    SID = "session-cap"
    TS = 1_700_000_000

    # Each list, the ceiling it claims, and the columns a row of it needs.
    LISTS = (
        (
            "bash",
            300,
            ("tool_name", "bash_cmd", "success"),
            ("tool_result", "Bash", "echo hello", "true"),
        ),
        (
            "errors",
            200,
            ("tool_name", "success", "error_type"),
            ("tool_result", "Bash", "false", "ShellError"),
        ),
        (
            "api_errors",
            200,
            ("attrs",),
            ("api_error", '{"status_code": 529, "error": "Overloaded"}'),
        ),
        (
            "subagents",
            300,
            ("attrs",),
            ("subagent_completed", '{"agent_type": "Explore"}'),
        ),
    )

    def _insert(self, n, columns, row):
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts, name, session_id, project, host, %s) "
                "VALUES (?,?,?,?,?%s)" % (", ".join(columns), ", ?" * len(columns)),
                [
                    (self.TS + i, row[0], self.SID, "testproj", "testhost") + row[1:]
                    for i in range(n)
                ],
            )

    def test_each_analysis_list_stops_at_the_number_it_claims(self):
        for section, cap, columns, row in self.LISTS:
            with self.subTest(section=section):
                self._insert(cap + 1, columns, row)
                payload = api_analysis(NO_FILTER)
                self.assertEqual(len(payload[section]), cap)
                self.assertIn(section, payload["truncated"])

    def _seed_prompts(self, n):
        """One turn per row, so the ids have to differ: the listing groups on
        prompt_id, and n identical ones are a single turn."""
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts, name, session_id, project, host, prompt_id) "
                "VALUES (?,'user_prompt',?,'testproj','testhost',?)",
                [(self.TS + i, self.SID, "prompt-%03d" % i) for i in range(n)],
            )

    def test_the_prompt_list_stops_at_three_hundred_of_the_newest_turns(self):
        """The listing is read newest first, so the turn the cut drops is the
        oldest one -- not whichever the grouping happened to reach last."""
        self._seed_prompts(301)
        payload = api_analysis(NO_FILTER)
        self.assertEqual(len(payload["prompts"]), 300)
        self.assertIn("prompts", payload["truncated"])
        ids = [p["prompt_id"] for p in payload["prompts"]]
        self.assertEqual(ids[0], "prompt-300")
        self.assertNotIn("prompt-000", ids)

    def test_the_hook_fire_list_stops_at_two_hundred(self):
        self._insert(
            201,
            ("hook_name", "hook_event"),
            ("hook_execution_complete", "PreToolUse:Bash", "PreToolUse"),
        )
        self.assertEqual(len(api_hook("PreToolUse:Bash")["fires"]), 200)


class TestTheAttrsBlobLivesInItsSibling(BaseDBTest):
    """The raw attribute blob is stored in event_attrs, not on the events row
    every aggregate scans (#180). The inspector reads it back by JOIN, and the
    events.attrs column it left behind stays NULL on every ingested row."""

    def test_the_blob_lands_in_the_sibling_and_not_on_the_events_row(self):
        seed_log_event(
            "tool_result",
            "sess-blob",
            [
                _attr("tool_name", _str("Read")),
                _attr("tool_input", _str(json.dumps({"file_path": "/w/a.py"}))),
            ],
        )
        row = store.query_row(
            "SELECT events.attrs AS on_row, event_attrs.attrs AS sibling "
            "FROM events LEFT JOIN event_attrs ON event_attrs.event_id = events.id"
        )
        self.assertIsNone(row["on_row"])
        self.assertEqual(
            json.loads(store.attrs_json(row["sibling"]))["tool_name"], "Read"
        )

    def test_the_inspector_reads_the_blob_through_the_join(self):
        seed_log_event(
            "tool_result",
            "sess-blob",
            [
                _attr("tool_name", _str("Edit")),
                _attr("tool_input", _str(json.dumps({"file_path": "/w/a.py"}))),
            ],
        )
        eid = store.query_value("SELECT id FROM events")
        event = api_event(eid)
        self.assertEqual(event["attrs"]["tool_name"], "Edit")
        self.assertEqual(event["tool_input"]["file_path"], "/w/a.py")

    def test_an_event_that_stored_no_blob_inspects_to_none(self):
        # hook_execution_start writes no blob, so it has no sibling row at all.
        seed_log_event("hook_execution_start", "sess-blob")
        eid = store.query_value("SELECT id FROM events")
        self.assertIsNone(api_event(eid)["attrs"])


class TestToolStatsPercentilesInSql(BaseDBTest):
    """Median and p95 of result_bytes computed in SQL, so the endpoint never
    reads the whole tool_result set into Python. The nearest-rank index matches
    the old pctile: round(fraction * (n - 1))."""

    SID = "session-pctile"

    def setUp(self):
        super().setUp()
        for size in (10, 20, 30, 40, 50):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("tool_name", _str("Bash")),
                    _attr("success", _str("true")),
                    _attr("tool_result_size_bytes", _str(str(size))),
                ],
            )

    def test_median_and_p95_are_the_nearest_rank_values(self):
        tool = next(t for t in api_analysis(NO_FILTER)["tools"] if t["label"] == "Bash")
        # n=5: median index round(0.5*4)=2 -> 30; p95 index round(0.95*4)=4 -> 50.
        self.assertEqual(tool["median_bytes"], 30)
        self.assertEqual(tool["p95"], 50)

    def test_a_label_with_no_sized_call_reports_zero_not_a_missing_key(self):
        seed_log_event(
            "tool_result",
            self.SID,
            [_attr("tool_name", _str("Read")), _attr("success", _str("true"))],
        )
        tool = next(t for t in api_analysis(NO_FILTER)["tools"] if t["label"] == "Read")
        self.assertEqual((tool["median_bytes"], tool["p95"]), (0, 0))


class TestHealthIsBoundByTheWindow(BaseDBTest):
    """/api/health honours the reader's day window: a counter over events or
    metric_points drops the rows older than it. Host and project stay global --
    narrowing those would hide the misconfigured one the page exists to find."""

    SID = "session-health-window"

    def setUp(self):
        super().setUp()
        seed_log_event(
            "user_prompt",
            self.SID,
            [_attr("prompt", _str("recent")), _attr("prompt.id", _str("p-recent"))],
        )
        seed_log_event(
            "user_prompt",
            self.SID,
            [_attr("prompt", _str("old")), _attr("prompt.id", _str("p-old"))],
            seconds_ago=30 * 86400,
        )

    def test_the_window_drops_the_older_prompt(self):
        week = api_health(Filters(days=7, host=None, project=None))
        self.assertEqual(week["prompts_total"], 1)
        self.assertEqual(api_health(NO_FILTER)["prompts_total"], 2)

    def test_host_and_project_do_not_narrow_the_page(self):
        # A filter naming another host still sees both prompts: Diagnostics is
        # global across hosts and projects by design.
        other = api_health(Filters(days=0, host="nowhere", project="nowhere"))
        self.assertEqual(other["prompts_total"], 2)


if __name__ == "__main__":
    unittest.main()
