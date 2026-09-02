# Run: python3 -m unittest discover -s tests -v  (from repo root)
import contextlib
import io
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from dataclasses import replace

from base import BaseDBTest
from test_api import NO_FILTER

from ccdash.core.aggregates import (
    WEIGHTS,
    scoped,
    short_model,
    success_bool,
    windowed,
)
from ccdash.core.request import Scope
from ccdash.pages.sessions import session_figures

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdash import ingest
from ccdash.core import store


class TestModuleSurface(unittest.TestCase):
    def test_importing_api_does_not_load_the_entrypoint(self):
        """Run in a subprocess, the only place `sys.modules` is a clean slate.

        An arrow back to the entrypoint would make `python3 -m ccdash` -- where
        `__main__` runs `server` -- load a second copy of it, so two distinct
        store connections, the endpoints' one left at None. The tests import
        `server` the normal way and would never see it.
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.path.insert(0, %r); "
                "from ccdash import api, ingest; from ccdash.core import store; "
                "print('ccdash.server' in sys.modules)" % root,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(out.stdout.strip(), "False")


class TestBaseIsolation(BaseDBTest):
    def test_base_isolates_on_the_real_connection(self):
        opened = store.query_row("PRAGMA database_list")["file"]
        self.assertEqual(os.path.realpath(opened), os.path.realpath(self.db_path))


class TestAnyValue(unittest.TestCase):
    """Tests for the anyvalue function (OTLP AnyValue decoding)."""

    def test_each_otlp_type_decodes_to_its_python_value(self):
        """The type is asserted alongside the value: `assertEqual(x, True)`
        would pass for 1, and an intValue read as a string would compare equal
        to nothing the callers do arithmetic on."""
        cases = [
            ("stringValue", {"stringValue": "hello"}, "hello"),
            ("boolValue true", {"boolValue": True}, True),
            ("boolValue false", {"boolValue": False}, False),
            # intValue can be a string in OTLP/JSON.
            ("intValue as a string", {"intValue": "42"}, 42),
            ("intValue as an int", {"intValue": 7}, 7),
            ("doubleValue", {"doubleValue": 3.14}, 3.14),
            ("arrayValue without values", {"arrayValue": {}}, []),
            (
                "arrayValue with items",
                {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}},
                ["a", 1],
            ),
            (
                "kvlistValue",
                {
                    "kvlistValue": {
                        "values": [
                            {"key": "k1", "value": {"stringValue": "v1"}},
                            {"key": "k2", "value": {"intValue": "99"}},
                        ]
                    }
                },
                {"k1": "v1", "k2": 99},
            ),
            ("a bare string passes through", "raw", "raw"),
            ("a bare int passes through", 123, 123),
            ("None passes through", None, None),
        ]
        for label, payload, expected in cases:
            with self.subTest(label=label):
                result = ingest.anyvalue(payload)
                self.assertEqual(result, expected)
                self.assertIs(type(result), type(expected))

    def test_nested_array_in_array(self):
        v = {
            "arrayValue": {
                "values": [{"arrayValue": {"values": [{"stringValue": "x"}]}}]
            }
        }
        self.assertEqual(ingest.anyvalue(v), [["x"]])

    def test_unknown_dict_returns_none(self):
        self.assertIsNone(ingest.anyvalue({"unknownKey": "val"}))

    def test_int_value_bad_string(self):
        # When the value cannot be converted, returns the raw value
        result = ingest.anyvalue({"intValue": "not_a_number"})
        self.assertEqual(result, "not_a_number")


class TestKvlist(unittest.TestCase):
    """Tests for kvlist: dict building + DROP_ATTRS filtering."""

    def test_basic(self):
        items = [
            {"key": "session.id", "value": {"stringValue": "sess-1"}},
            {"key": "model", "value": {"stringValue": "claude-opus-4"}},
        ]
        result = ingest.kvlist(items)
        self.assertEqual(result["session.id"], "sess-1")
        self.assertEqual(result["model"], "claude-opus-4")

    def test_drops_pii_keys(self):
        items = []
        for k in ingest.DROP_ATTRS:
            items.append({"key": k, "value": {"stringValue": "secret"}})
        items.append({"key": "safe_key", "value": {"stringValue": "ok"}})
        result = ingest.kvlist(items)
        for k in ingest.DROP_ATTRS:
            self.assertNotIn(k, result)
        self.assertIn("safe_key", result)

    def test_empty_list(self):
        self.assertEqual(ingest.kvlist([]), {})

    def test_none_list(self):
        self.assertEqual(ingest.kvlist(None), {})

    def test_drop_attrs_contains_expected_keys(self):
        expected = {
            "user.email",
            "user.id",
            "user.account_uuid",
            "user.account_id",
            "organization.id",
            "user.groups",
        }
        self.assertEqual(ingest.DROP_ATTRS, expected)


class TestShortModel(unittest.TestCase):
    """Tests for short_model."""

    def test_every_known_family_is_named(self):
        cases = [
            ("claude-opus-4", "Opus"),
            ("claude-sonnet-4-5", "Sonnet"),
            ("claude-haiku-3", "Haiku"),
            ("claude-fable-1", "Fable"),
            ("claude-mythos-1", "Mythos"),
        ]
        for model, expected in cases:
            with self.subTest(model=model):
                self.assertEqual(short_model(model), expected)

    def test_unknown_model(self):
        # Unknown model: takes the first part after claude- and capitalizes it
        result = short_model("claude-future-99")
        self.assertEqual(result, "Future")

    def test_none_model(self):
        self.assertIsNone(short_model(None))

    def test_empty_string(self):
        self.assertIsNone(short_model(""))

    def test_no_prefix(self):
        # Model without the claude- prefix
        result = short_model("opus-3")
        self.assertEqual(result, "Opus")


class TestSuccessBool(unittest.TestCase):
    """Tests for success_bool."""

    def test_true_string(self):
        self.assertIs(success_bool("true"), True)

    def test_false_string(self):
        self.assertIs(success_bool("false"), False)

    def test_none_stays_none(self):
        self.assertIsNone(success_bool(None))

    def test_unknown_string_is_none(self):
        self.assertIsNone(success_bool(""))
        self.assertIsNone(success_bool("1"))


class TestAsIntFloat(unittest.TestCase):
    """as_int and as_float are `int()` and `float()` behind a try/except: the
    conversion is the standard library's, the fallback to None is ours."""

    def test_an_unreadable_value_is_none_rather_than_a_raise(self):
        cases = [
            ("as_int of None", store.as_int, None),
            ("as_int of a word", store.as_int, "abc"),
            ("as_int of a list", store.as_int, []),
            ("as_float of None", store.as_float, None),
            ("as_float of a word", store.as_float, "xyz"),
            ("as_float of a dict", store.as_float, {}),
        ]
        for label, convert, value in cases:
            with self.subTest(label=label):
                self.assertIsNone(convert(value))


class TestNanoToS(unittest.TestCase):
    """Tests for nano_to_s."""

    def test_known_value(self):
        # 1_000_000_000 ns = 1 s (epoch)
        self.assertEqual(ingest.nano_to_s(1_000_000_000), 1)

    def test_string_nano(self):
        self.assertEqual(ingest.nano_to_s("2000000000"), 2)
        # A realistic timestamp: 2024-01-01 in nanoseconds.
        self.assertEqual(ingest.nano_to_s(str(1704067200 * 1_000_000_000)), 1704067200)

    def test_bad_input_returns_current_time_and_says_so(self):
        import time

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            result = ingest.nano_to_s("not_a_number")
        self.assertAlmostEqual(result, int(time.time()), delta=5)
        self.assertIn("not_a_number", err.getvalue())

    def test_readable_nano_logs_nothing(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ingest.nano_to_s(1_000_000_000)
        self.assertEqual(err.getvalue(), "")


class TestParseParams(unittest.TestCase):
    """Tests for parse_params."""

    def test_dict_passthrough(self):
        a = {"tool_parameters": {"key": "val"}}
        self.assertEqual(ingest.parse_params(a), {"key": "val"})

    def test_json_string(self):
        a = {"tool_parameters": '{"cmd": "ls"}'}
        self.assertEqual(ingest.parse_params(a), {"cmd": "ls"})

    def test_garbage_string(self):
        for raw in ("not json at all", "{bad json"):
            with self.subTest(raw=raw):
                self.assertEqual(ingest.parse_params({"tool_parameters": raw}), {})

    def test_missing_key(self):
        a = {}
        self.assertEqual(ingest.parse_params(a), {})


class TestMakeLabel(unittest.TestCase):
    """Tests for make_label."""

    def test_mcp_label(self):
        p = {"mcp_server_name": "myserver", "mcp_tool_name": "mytool"}
        self.assertEqual(ingest.make_label("mcp_tool", p), "mcp:myserver/mytool")

    def test_mcp_label_no_tool_name(self):
        p = {"mcp_server_name": "myserver"}
        self.assertEqual(ingest.make_label("mcp_tool", p), "mcp:myserver/?")

    def test_skill_label(self):
        p = {"skill_name": "deep-research"}
        self.assertEqual(ingest.make_label("some_tool", p), "skill:deep-research")

    def test_agent_label(self):
        p = {"subagent_type": "code"}
        self.assertEqual(ingest.make_label("Task", p), "agent:code")

    def test_native_tool(self):
        p = {}
        self.assertEqual(ingest.make_label("Bash", p), "Bash")

    def test_a_record_that_is_not_a_tool_call_has_no_label(self):
        # Hook fires, API requests and prompts all reach the ingester without a
        # tool_name. A placeholder there would be stored, indexed and grouped as
        # though it were a tool of its own.
        self.assertIsNone(ingest.make_label(None, {}))

    def test_mcp_priority_over_skill(self):
        # mcp_server_name takes priority over skill_name
        p = {"mcp_server_name": "srv", "mcp_tool_name": "t", "skill_name": "x"}
        result = ingest.make_label("mcp_tool", p)
        self.assertTrue(result.startswith("mcp:"))


class TestFiltersScope(unittest.TestCase):
    def test_an_unset_filter_leaves_the_clause_empty(self):
        """days=0 and host=None are the values a request without parameters
        carries: an empty scope, not a clause matching nothing."""
        cases = [
            ("nothing set", NO_FILTER),
            ("days=0", replace(NO_FILTER, days=0)),
            ("host=None", replace(NO_FILTER, host=None)),
        ]
        for label, filters in cases:
            with self.subTest(label=label):
                scope = filters.scope()
                self.assertEqual(scope.clause, "")
                self.assertEqual(scope.args, ())

    def test_a_set_filter_reaches_the_clause_and_its_args(self):
        cases = [
            ("days", replace(NO_FILTER, days=7), ("ts >=", "7 days"), ()),
            ("host", replace(NO_FILTER, host="myhost"), ("host = ?",), ("myhost",)),
            (
                "project",
                replace(NO_FILTER, project="myproject"),
                ("project = ?",),
                ("myproject",),
            ),
            (
                "all three",
                replace(NO_FILTER, days=30, host="h1", project="proj"),
                ("ts >=", "host = ?", "project = ?"),
                ("h1", "proj"),
            ),
        ]
        for label, filters, fragments, args in cases:
            with self.subTest(label=label):
                scope = filters.scope()
                for fragment in fragments:
                    self.assertIn(fragment, scope.clause)
                self.assertEqual(scope.args, args)

    def test_previous_window_slides_back(self):
        # Twice the window and once it, so the clause covers the 7 days before
        # the last 7.
        clause = replace(NO_FILTER, days=7).scope(previous=True).clause
        self.assertIn("-14 days", clause)
        self.assertIn("-7 days", clause)


class TestScope(unittest.TestCase):
    def test_narrow_appends_clause_and_args(self):
        scope = Scope(" AND session_id=?", ("s1",)).narrow(" AND label=?", "Bash")
        self.assertEqual(scope.clause, " AND session_id=? AND label=?")
        self.assertEqual(scope.args, ("s1", "Bash"))

    def test_narrow_leaves_the_original_alone(self):
        # Frozen: a drill-down narrowing its scope cannot alter the window it
        # was built from.
        scope = Scope(" AND session_id=?", ("s1",))
        scope.narrow(" AND label=?", "Bash")
        self.assertEqual(scope.args, ("s1",))

    def test_unbounded_is_the_only_empty_scope(self):
        self.assertEqual(Scope.UNBOUNDED.clause, "")
        self.assertEqual(Scope.UNBOUNDED.args, ())

    def test_scope_takes_no_empty_default(self):
        # The window is never an omitted argument: a bare Scope() would be an
        # unbounded scan nobody named.
        with self.assertRaises(TypeError):
            Scope()  # type: ignore[call-arg]


class TestWindowed(unittest.TestCase):
    def test_one_marker_fills_the_window_and_appends_its_args(self):
        scope = Scope(" AND host=?", ("h1",))
        sql, args = windowed("SELECT 1 FROM t WHERE 1{scope}", scope)
        self.assertEqual(sql, "SELECT 1 FROM t WHERE 1 AND host=?")
        self.assertEqual(args, ("h1",))

    def test_each_marker_repeats_the_args_in_order(self):
        scope = Scope(" AND host=?", ("h1",))
        sql, args = windowed("A{scope} UNION B{scope}", scope)
        self.assertEqual(sql, "A AND host=? UNION B AND host=?")
        self.assertEqual(args, ("h1", "h1"))

    def test_own_args_precede_the_window(self):
        scope = Scope(" AND host=?", ("h1",))
        sql, args = windowed("SELECT 1 FROM t WHERE name=?{scope}", scope, ("n",))
        self.assertEqual(args, ("n", "h1"))

    def test_a_markerless_template_is_a_refused_scan(self):
        # A windowed helper with nowhere to put the window is the silent scan
        # this seam removes: it must fail, not run unbounded.
        scope = Scope(" AND host=?", ("h1",))
        with self.assertRaises(ValueError):
            windowed("SELECT 1 FROM t", scope)

    def test_unbounded_renders_no_clause(self):
        sql, args = windowed("SELECT 1 FROM t WHERE 1{scope}", Scope.UNBOUNDED)
        self.assertEqual(sql, "SELECT 1 FROM t WHERE 1")
        self.assertEqual(args, ())


class TestScoped(unittest.TestCase):
    def test_assembles_select_from_population_with_the_window(self):
        scope = Scope(" AND host=?", ("h1",))
        sql, args = scoped("a, COUNT(*) n", "t WHERE name='x'{scope}", scope)
        self.assertEqual(sql, "SELECT a, COUNT(*) n FROM t WHERE name='x' AND host=?")
        self.assertEqual(args, ("h1",))

    def test_tail_clauses_land_in_sql_order(self):
        sql, _ = scoped(
            "a",
            "t WHERE 1{scope}",
            Scope.UNBOUNDED,
            group="a",
            order="n DESC",
            limit=5,
        )
        self.assertEqual(
            sql, "SELECT a FROM t WHERE 1 GROUP BY a ORDER BY n DESC LIMIT 5"
        )

    def test_population_args_precede_the_window(self):
        scope = Scope(" AND host=?", ("h1",))
        _, args = scoped("a", "t WHERE name=?{scope}", scope, args=("n",))
        self.assertEqual(args, ("n", "h1"))

    def test_nested_marker_repeats_the_window(self):
        scope = Scope(" AND host=?", ("h1",))
        _, args = scoped("COUNT(*)", "(SELECT id FROM t WHERE 1{scope}){scope}", scope)
        self.assertEqual(args, ("h1", "h1"))

    def test_a_population_without_a_marker_is_refused(self):
        # No marker means no window even under a real scope: the slip must fail.
        with self.assertRaises(ValueError):
            scoped("a", "t WHERE name='x'", Scope(" AND host=?", ("h1",)))


class TestQueryValue(BaseDBTest):
    """Tests for query_value(sql, args): first column of the first row."""

    def insert_events(self, count):
        with store.write() as db:
            for i in range(count):
                db.execute(
                    "INSERT INTO events (ts, name) VALUES (?, ?)",
                    (1700000000 + i, "tool_result"),
                )

    def test_first_column_by_position(self):
        self.insert_events(3)
        # Unaliased: the column is read by position, so none is required.
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 3)
        # Aliased: a query naming its column reads the same.
        self.assertEqual(store.query_value("SELECT COUNT(*) v FROM events"), 3)
        # An aggregate over nothing: one row holding NULL, so this pins `r[0] or 0`.
        self.assertEqual(
            store.query_value("SELECT SUM(ts) FROM events WHERE name='nope'"), 0
        )

    def test_zero_rows_is_zero_not_a_crash(self):
        # Not the same case as an aggregate over nothing, which still returns a
        # row: here query_row hands back None and the `if r` branch is the only
        # thing between the caller and a TypeError.
        self.insert_events(3)
        self.assertEqual(
            store.query_value("SELECT ts FROM events WHERE name='nope'"), 0
        )
        self.assertEqual(store.query_value("SELECT ts FROM events LIMIT 0"), 0)


class TestWrite(BaseDBTest):
    """write() is the only way to the connection, and the only thing pairing it
    with its lock."""

    def test_a_clean_exit_commits(self):
        with store.write() as db:
            db.execute("INSERT INTO events (ts, name) VALUES (1700000000, 'x')")
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 1)

    def test_an_exception_rolls_back_and_re_raises(self):
        with self.assertRaises(ValueError):
            with store.write() as db:
                db.execute("INSERT INTO events (ts, name) VALUES (1700000000, 'x')")
                raise ValueError("boom")
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 0)

    def test_a_failed_write_leaves_no_transaction_for_the_next_one(self):
        """The rollback is what keeps a batch that raised from joining the next
        one: without it the open transaction survives on the connection."""
        with self.assertRaises(sqlite3.IntegrityError):
            with store.write() as db:
                db.execute("INSERT INTO events (ts, name) VALUES (1700000000, 'x')")
                db.execute("INSERT INTO events (ts, name) VALUES (NULL, 'y')")
        with store.write() as db:
            db.execute("INSERT INTO events (ts, name) VALUES (1700000001, 'z')")
        rows = store.query("SELECT name FROM events")
        self.assertEqual([r["name"] for r in rows], ["z"])

    def test_the_lock_is_released_after_a_failure(self):
        with self.assertRaises(ValueError):
            with store.write():
                raise ValueError("boom")
        with store.write() as db:
            db.execute("INSERT INTO events (ts, name) VALUES (1700000000, 'x')")
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 1)


class TestDbModes(unittest.TestCase):
    """The database holds prompt text, shell commands and absolute paths, so
    db_init narrows what the umask left open."""

    def setUp(self):
        self.dir = os.path.join(tempfile.mkdtemp(prefix="ccdash_mode_"), "ccdash")
        self.db_path = os.path.join(self.dir, "ccdash.db")

    def tearDown(self):
        store.db_close()
        shutil.rmtree(os.path.dirname(self.dir))

    def modes(self):
        return [
            stat.S_IMODE(os.stat(self.db_path + s).st_mode)
            for s in ("", "-wal", "-shm")
        ]

    def test_a_fresh_database_and_its_directory_are_private(self):
        store.db_init(self.db_path)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        self.assertEqual(self.modes(), [0o600, 0o600, 0o600])

    def test_a_group_readable_database_is_narrowed_in_a_directory_left_alone(self):
        """A directory ccdash did not create is the operator's -- `--db` names a
        project checkout as happily as `~/.ccdash`."""
        os.mkdir(self.dir, 0o755)
        store.db_init(self.db_path)
        store.db_close()
        os.chmod(self.db_path, 0o640)
        store.db_init(self.db_path)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o755)
        self.assertEqual(self.modes(), [0o600, 0o600, 0o600])

    def test_a_chmod_that_fails_warns_and_boots(self):
        err = io.StringIO()
        with unittest.mock.patch("os.chmod", side_effect=OSError("read-only")):
            with contextlib.redirect_stderr(err):
                store.db_init(self.db_path)
        self.assertIn(self.db_path, err.getvalue())
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 0)


class TestDbClose(BaseDBTest):
    def test_closing_twice_leaves_the_next_init_free_to_reopen(self):
        """tearDown calls db_close on every test, including the ones that closed
        the connection themselves, so the second call has to be a no-op rather
        than something the following init inherits."""
        store.db_close()
        store.db_close()
        store.db_init(self.db_path)
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 0)


class TestSessionFigures(unittest.TestCase):
    """session_figures derives the two figures the session list ranks on. It is
    a pure function of the two dicts handed to it, so it needs no database."""

    def test_tokens_and_output_weight(self):
        row = {
            "input_tokens": 1000,
            "cache_read_tokens": 500,
            "cache_creation_tokens": 200,
            "output_tokens": 300,
            "cost": 1.0,
            "lines_added": 5,
            "lines_removed": 2,
            "active_seconds": 60,
        }
        session_figures(row, {"tools": 3, "compactions": 1, "prompts": 2})
        self.assertEqual(row["tokens"], 2000)
        w = WEIGHTS
        wsum = (
            1000 * w["input"]
            + 500 * w["cache_read"]
            + 200 * w["cache_creation"]
            + 300 * w["output"]
        )
        self.assertAlmostEqual(row["output_weight_pct"], 100 * 300 * w["output"] / wsum)
        self.assertEqual(row["compactions"], 1)

    def test_an_idle_session_weighs_zero_rather_than_dividing_by_zero(self):
        row = {
            "input_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
            "output_tokens": 0,
            "cost": 0,
            "lines_added": 0,
            "lines_removed": 0,
            "active_seconds": 0,
        }
        session_figures(row, {})
        self.assertEqual(row["output_weight_pct"], 0)
        self.assertEqual(row["tokens"], 0)


if __name__ == "__main__":
    unittest.main()
