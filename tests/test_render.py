# Run: python3 -m unittest discover -s tests -v  (from repo root)
"""Executes the frontend renderers and fails on render defects.

Every other frontend test in this suite scans the source as text. That cannot
see a column key left on a payload field the backend stopped emitting, a spread
on a parameter that was renamed, or a bar fed the wrong property: all three are
valid JavaScript. They only exist once the template has run.

So this file seeds a database, calls the real API functions, and hands their
payloads to `tests/render.mjs`, which imports the modules as they are served and
returns the HTML. The assertions are on defect *symptoms* -- `NaN`, `undefined`,
`[object Object]`, an empty `data-id`, a sort key naming nothing -- rather than
on markup, so restyling a table does not fail a test.

Requires `node`, already the project's tool for `node --check`. No dependency,
no build step: the modules are executed exactly as the browser gets them.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base import BaseDBTest
from test_api import NO_FILTER, _attr, _dbl, _int, _str, seed_log_event, seed_metric

from ccdash import ingest
from ccdash.core import store
from ccdash.core.request import NOT_FOUND, Scope
from ccdash.pages.analysis import api_analysis, api_calls, delegation_types
from ccdash.pages.costs import api_costs, api_projects
from ccdash.pages.details import api_prompt
from ccdash.pages.health import api_health, api_hook
from ccdash.pages.overview import api_overview
from ccdash.pages.sessions import api_context, api_session, api_sessions

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(TESTS_DIR, "render.mjs")
APP_MJS = os.path.join(os.path.dirname(TESTS_DIR), "ccdash", "web", "assets", "app.mjs")

# The table id suffix of each ROW_MODALS entry, `["apierr", "ev", (id) => ...]`.
# app.mjs wires the document at load and cannot be imported, so the list is read
# as text and checked against what the renderers produce.
ROW_MODAL_SUFFIX = re.compile(r'^\s*\["([a-z]+)", "[a-z]+", \(id\)', re.M)
# `if (table.endsWith("files"))` -- the branches `handleRowClick` falls through
# to, for the two tables whose modal needs a scope no ROW_MODALS entry carries.
FALLBACK_SUFFIX = re.compile(r'table\.endsWith\("([a-z]+)"\)')

# A payload that is markup rather than a value. Distinct per field, so a cell
# that stopped escaping names itself.
XSS = "<img src=x id=%s onerror=alert(1)>"

# Symptoms of a renderer handed a field that is not there. `undefined` and `NaN`
# come from reading a missing property; `[object Object]` from interpolating a
# row where a scalar was meant.
DEFECT_MARKERS = ("NaN", "undefined", "[object Object]", "Infinity")

TABLE_RE = re.compile(r"<table data-t=(\S+?)>(.*?)</table>", re.S)

# The 29 tables the harness jobs draw, pages and modals together. It pins what
# those jobs cover rather than listing the dashboard: a table no job exercises
# is outside its reach.
DASHBOARD_TABLES = {
    "acalls",
    "en",
    "gapierr",
    "gbashd",
    "gctx",
    "gdec",
    "gerrd",
    "ginv",
    "gprompts",
    "gsubc",
    "gtools",
    "hk",
    "hkfires",
    "idl",
    "ing",
    "mn",
    "nt",
    "phk",
    "psubc",
    "ptools",
    "sapierr",
    "sbashd",
    "sdec",
    "serrd",
    "sfiles",
    "sinv",
    "sprompts",
    "ssubc",
    "stools",
}
SORT_KEY_RE = re.compile(r'<th data-k="([^"]*)"')
HEADER_RE = re.compile(r'<th data-k="([^"]*)" class="([^"]*)">')
ROW_ID_RE = re.compile(r'<tr data-id="([^"]*)"')

# `invTable` builds its rows itself, tagging each with the list it came from, so
# `type` is the one sort key with no counterpart in any payload.
SYNTHETIC_SORT_KEYS = {"type"}


def render(jobs):
    """Runs the renderers named in `jobs` under node, in one process.

    `jobs` is [(name, payload)]; the return is {name: html}. A renderer that
    throws comes back as {"error": ...} and is reported by the caller, since a
    throw is itself the defect this file is here to catch.
    """
    proc = subprocess.run(
        ["node", HARNESS],
        input=json.dumps([{"name": n, "data": d} for n, d in jobs]),
        capture_output=True,
        text=True,
        cwd=TESTS_DIR,
    )
    if proc.returncode != 0:
        raise AssertionError("the render harness failed:\n" + proc.stderr)
    return json.loads(proc.stdout)


def payload_keys(payload):
    """Every dict key the payload carries, at any depth.

    A sort key has to name one of them: `renderTable` compares `row[col.key]`,
    so a key naming nothing sorts every row on `undefined` and leaves the table
    in place while the header claims it is sorted.
    """
    found = set()
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            found.update(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def seed_summarised_events(session_id):
    """Seeds one record of each kind whose timeline cell is built by hand.

    Two of several kinds, where the summary branches: a mode switch with and
    without its trigger, a slow hook and a fast one, a failed connection and a
    live one.
    """
    seed_log_event(
        "permission_mode_changed",
        session_id,
        [
            _attr("from_mode", _str("plan")),
            _attr("to_mode", _str("auto")),
            _attr("trigger", _str("shift_tab")),
        ],
    )
    # `trigger` is not always exported.
    seed_log_event(
        "permission_mode_changed",
        session_id,
        [
            _attr("from_mode", _str("auto")),
            _attr("to_mode", _str("default")),
        ],
    )
    seed_log_event(
        "at_mention",
        session_id,
        [
            _attr("mention_type", _str("file")),
            _attr("success", _str("false")),
        ],
    )
    seed_log_event(
        "hook_execution_complete",
        session_id,
        [
            _attr("hook_name", _str("Stop")),
            _attr("total_duration_ms", _str("4262")),
        ],
    )
    seed_log_event(
        "hook_execution_complete",
        session_id,
        [
            _attr("hook_name", _str("PreToolUse:Bash")),
            _attr("total_duration_ms", _str("52")),
        ],
    )
    seed_log_event(
        "mcp_server_connection",
        session_id,
        [
            _attr("status", _str("failed")),
            _attr("server_name", _str("soloscrum")),
            _attr("transport_type", _str("stdio")),
        ],
    )
    seed_log_event(
        "mcp_server_connection",
        session_id,
        [
            _attr("status", _str("connected")),
            _attr("server_name", _str("codegraph")),
            _attr("transport_type", _str("stdio")),
        ],
    )
    seed_log_event("internal_error", session_id, [_attr("error_name", _str("Error"))])
    seed_log_event(
        "api_error",
        session_id,
        [
            _attr("status_code", _int(529)),
            _attr("error", _str("Overloaded")),
            _attr("attempt", _int(11)),
        ],
    )
    seed_log_event(
        "api_retries_exhausted",
        session_id,
        [
            _attr("total_attempts", _str("11")),
            _attr("total_retry_duration_ms", _str("195164")),
        ],
    )
    # The one record served by the `hook_name` fallback of describeEvent: a fire
    # carries a summary of its own.
    seed_log_event(
        "hook_registered",
        session_id,
        [
            _attr("hook_name", _str("PostToolUse:Edit")),
        ],
    )
    seed_log_event("retention_sweep", session_id, [])


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestTheHarnessAnswersOnAnUnknownJob(unittest.TestCase):
    """A job name that is a member of `Object.prototype` still has no renderer.

    Keyed on an object literal, `constructor` and `toString` resolve up the
    prototype chain and get called: the harness answers a rendered object or
    "[object Undefined]", neither of which any assertion here reports, and a
    renderer one day renamed onto such a key would empty a test file silently.
    """

    def test_a_job_named_after_a_prototype_member_gets_no_renderer(self):
        names = ("constructor", "toString", "__proto__", "nope")
        out = render([(n, {}) for n in names])
        for name in names:
            with self.subTest(job=name):
                self.assertEqual(
                    out.get(name), {"error": "no renderer named %s" % name}
                )


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestDurationsCarryTheirUnits(unittest.TestCase):
    """Every figure a duration prints says what it counts.

    A bare pair reads as a clock time: "2h 05" is five minutes past two to a
    reader who does not know the formatter, and the dashboard prints it beside
    timestamps that really are clock times."""

    def test_every_figure_of_a_duration_names_its_unit(self):
        seconds = [0, 45, 90, 3600, 7500, 86399]
        self.assertEqual(
            render([("format:duration", seconds)])["format:duration"],
            ["0s", "45s", "1m 30s", "1h 00min", "2h 05min", "23h 59min"],
        )


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestRenderersRunOnARealPayload(BaseDBTest):
    """One seeded window, every page and every analysis tab rendered from it.

    The seed covers each list the tables of `tables.mjs` read: tool calls of
    three kinds, a shell failure, edited files, prompts, a delegation, a
    permission decision, a skill activation and an MCP call.
    """

    SID = "session-render"
    IDLE_SID = "session-idle"
    MODEL = "claude-opus-4-8"
    # `short_model` capitalises, which neutralises `constructor` and `toString`
    # but leaves a name starting with `_` intact. A model family is a payload
    # value, so this is what a lookup keyed on one has to survive.
    HOSTILE_MODEL = "__proto__-x"
    SKILL = "ref-doc"
    MCP_SERVER = "soloscrum"
    FILE = "/repo/app/ccdash.py"

    def setUp(self):
        super().setUp()
        self._seed()
        self.payloads = {
            "overview": api_overview(NO_FILTER),
            "projects": api_projects(NO_FILTER),
            "sessions": api_sessions(NO_FILTER),
            "costs": api_costs(NO_FILTER),
            "health": api_health(NO_FILTER),
            "analysis": api_analysis(NO_FILTER),
            "context": api_context(NO_FILTER),
            "session": api_session(self.SID),
        }
        self.jobs = [
            ("overview", self.payloads["overview"]),
            ("projects", self.payloads["projects"]),
            ("sessions", self.payloads["sessions"]),
            ("costs", self.payloads["costs"]),
            ("health", self.payloads["health"]),
            ("context@global", self.payloads["context"]),
        ]
        for tab in ("tools", "bash", "prompts", "agents", "misc"):
            self.jobs.append((tab + "@global", self.payloads["analysis"]))
        # The API errors table sits behind a nested tab: the default one is
        # Failures, so `misc` alone never renders it.
        self.jobs.append(("misc:apierr@global", self.payloads["analysis"]))
        self.jobs.append(("misc:dec@global", self.payloads["analysis"]))
        self.jobs.append(("misc:apierr@session", self.payloads["session"]))
        self.jobs.append(("misc:dec@session", self.payloads["session"]))
        for tab in ("flow", "files", "tools", "bash", "prompts", "agents", "misc"):
            self.jobs.append((tab + "@session", self.payloads["session"]))
        # The three modals. Each is opened on a click and fed one endpoint, so
        # the tables they carry are reachable from nothing else.
        self.jobs.append(("modal:hook", api_hook("PreToolUse")))
        self.jobs.append(("modal:prompt", api_prompt("p-render")))
        self.jobs.append(
            (
                "modal:calls",
                {"label": "Bash", "calls": api_calls("Bash", NO_FILTER)},
            )
        )
        self.rendered = render(self.jobs)

    def _seed(self):
        seed_metric("claude_code.session.count", 1, self.SID, self.MODEL)
        seed_metric("claude_code.cost.usage", 0.42, self.SID, self.MODEL)
        for attr_type, value in (
            ("input", 1000),
            ("output", 300),
            ("cacheRead", 500),
            ("cacheCreation", 200),
        ):
            seed_metric(
                "claude_code.token.usage", value, self.SID, self.MODEL, attr_type
            )
        seed_metric("claude_code.cost.usage", 0.01, self.SID, self.HOSTILE_MODEL)
        # A second day, and the hostile family absent from it. `stackedAreaChart`
        # returns early below two points, so with a single day the cost chart is
        # never drawn and nothing exercises its per-family reads.
        seed_metric(
            "claude_code.cost.usage", 0.02, self.SID, self.MODEL, seconds_ago=2 * 86400
        )
        seed_metric(
            "claude_code.lines_of_code.count", 30, self.SID, self.MODEL, "added"
        )
        seed_metric(
            "claude_code.lines_of_code.count", 10, self.SID, self.MODEL, "removed"
        )
        seed_metric("claude_code.active_time.total", 120, self.SID, self.MODEL)

        seed_log_event(
            "user_prompt",
            self.SID,
            [
                _attr("prompt", _str("Rename the payload keys")),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "api_request",
            self.SID,
            [
                _attr("cost_usd", _dbl(0.42)),
                _attr("input_tokens", _int(1000)),
                _attr("output_tokens", _int(300)),
                _attr("model", _str(self.MODEL)),
                # Styled, so the fixture carries the shape the session panel has to
                # print -- and the context curve still has to find it.
                _attr("query_source", _str("repl_main_thread:outputStyle:Concise")),
                _attr("effort", _str("high")),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("50")),
                _attr("tool_result_size_bytes", _str("300")),
                _attr(
                    "tool_parameters",
                    _str(
                        json.dumps(
                            {"full_command": "ls -la", "description": "list files"}
                        )
                    ),
                ),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("success", _str("false")),
                _attr("error_type", _str("ShellError")),
                _attr("duration_ms", _str("12")),
                _attr("tool_result_size_bytes", _str("40")),
                _attr(
                    "tool_parameters",
                    _str(json.dumps({"full_command": "make", "description": "build"})),
                ),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        for tool in ("Edit", "Write"):
            seed_log_event(
                "tool_result",
                self.SID,
                [
                    _attr("tool_name", _str(tool)),
                    _attr("success", _str("true")),
                    _attr("duration_ms", _str("30")),
                    _attr("tool_result_size_bytes", _str("120")),
                    _attr("tool_input", _str(json.dumps({"file_path": self.FILE}))),
                    _attr("prompt.id", _str("p-render")),
                ],
            )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("mcp_tool")),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("70")),
                _attr("tool_result_size_bytes", _str("900")),
                _attr(
                    "tool_parameters",
                    _str(
                        json.dumps(
                            {
                                "mcp_server_name": self.MCP_SERVER,
                                "mcp_tool_name": "list_ideas",
                            }
                        )
                    ),
                ),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "skill_activated",
            self.SID,
            [
                _attr("tool_parameters", _str(json.dumps({"skill_name": self.SKILL}))),
            ],
        )
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Task")),
                _attr("success", _str("true")),
                _attr("duration_ms", _str("9000")),
                _attr("tool_result_size_bytes", _str("2200")),
                _attr("agent_type", _str("general-purpose")),
                _attr(
                    "tool_parameters",
                    _str(json.dumps({"description": "measure the branch"})),
                ),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "subagent_completed",
            self.SID,
            [
                _attr("agent_type", _str("general-purpose")),
                _attr("model", _str(self.MODEL)),
                _attr("total_tokens", _int(4200)),
                _attr("total_tool_uses", _int(7)),
                _attr("duration_ms", _str("9000")),
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "tool_decision",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("decision", _str("accept")),
                _attr("source", _str("config")),
            ],
        )
        seed_log_event(
            "compaction",
            self.SID,
            [
                _attr("trigger", _str("auto")),
                _attr("pre_tokens", _int(150000)),
                _attr("post_tokens", _int(20000)),
            ],
        )
        seed_log_event(
            "hook",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse")),
                _attr("decision", _str("accept")),
            ],
        )
        # Feeds the Hooks table of Diagnostics: `hook_stats` reads this record
        # alone, so without it the health page renders no hook table at all.
        seed_log_event(
            "hook_execution_complete",
            self.SID,
            [
                _attr("hook_name", _str("PreToolUse")),
                _attr("hook_event", _str("PreToolUse")),
                _attr("total_duration_ms", _str("52")),
                # Also feeds the Hooks table of the prompt modal, which is scoped on
                # the turn rather than global.
                _attr("prompt.id", _str("p-render")),
            ],
        )
        seed_log_event(
            "api_error",
            self.SID,
            [
                _attr("status_code", _int(529)),
                _attr("error", _str("Overloaded")),
                _attr("attempt", _int(2)),
                _attr("duration_ms", _str("900")),
                _attr("model", _str(self.MODEL)),
            ],
        )
        # The records whose timeline cell is built by hand, so the sweeps of
        # this class read the summaries too.
        seed_summarised_events(self.SID)
        # Two boxes of Diagnostics render for nobody otherwise: Idle sessions
        # needs a session that spent nothing, Ingestion errors a refused batch.
        # `log_ingest` writes that log, under the ingestion functions.
        seed_metric("claude_code.session.count", 1, self.IDLE_SID)
        ingest.log_ingest("metrics", 4, 0)
        ingest.log_ingest("logs", 2, 1, "unparsable batch")

    def test_no_renderer_throws(self):
        for name, html in self.rendered.items():
            with self.subTest(renderer=name):
                self.assertIsInstance(
                    html,
                    str,
                    "%s threw: %s"
                    % (name, html.get("error") if isinstance(html, dict) else html),
                )
                self.assertTrue(html.strip(), "%s rendered nothing" % name)

    def test_no_renderer_prints_a_missing_field(self):
        for name, html in self.rendered.items():
            if not isinstance(html, str):
                continue
            for marker in DEFECT_MARKERS:
                with self.subTest(renderer=name, marker=marker):
                    self.assertNotIn(marker, html)

    def test_the_harness_draws_every_table_the_dashboard_has(self):
        """The 29 tables `docs/frontend.md` claims the harness covers, by name.

        On the set and not on the count: a renamed table reads as a different
        table, where an equal cardinal would hide it.
        """
        drawn = set()
        for html in self.rendered.values():
            if not isinstance(html, str):
                continue
            drawn.update(table_id for table_id, _ in TABLE_RE.findall(html))
        self.assertEqual(drawn, DASHBOARD_TABLES)

    def test_every_sort_key_names_a_field_its_payload_carries(self):
        by_renderer = dict(self.jobs)
        seen = 0
        for name, html in self.rendered.items():
            if not isinstance(html, str):
                continue
            known = payload_keys(by_renderer[name]) | SYNTHETIC_SORT_KEYS
            for table_id, body in TABLE_RE.findall(html):
                for key in SORT_KEY_RE.findall(body):
                    seen += 1
                    with self.subTest(renderer=name, table=table_id, key=key):
                        self.assertIn(key, known)
        # Without this the whole test passes on an empty dashboard.
        self.assertGreaterEqual(seen, 40, "no sort key read -- the scan is vacuous")

    def test_the_default_sort_never_names_a_hidden_column(self):
        """The column a table opens on has to be one a phone shows. `renderTable`
        falls back to `cols[1]` when no `first` is passed, so a table whose second
        column declares `hide` opens below 768px on a header nobody can see or
        click: the order looks arbitrary and no other column is sorted."""
        seen = 0
        for name, html in self.rendered.items():
            if not isinstance(html, str):
                continue
            for table_id, body in TABLE_RE.findall(html):
                for key, classes in HEADER_RE.findall(body):
                    tokens = classes.split()
                    if "s" not in tokens:
                        continue
                    # HEADER_RE is fixed on the attribute order `renderTable`
                    # writes, so a third attribute on the `<th>` would empty this
                    # loop and leave a count of tables saying the scan ran.
                    seen += 1
                    with self.subTest(renderer=name, table=table_id, key=key):
                        self.assertNotIn(
                            "max-md:hidden",
                            tokens,
                            "%s opens sorted on %s, hidden below 768px"
                            % (table_id, key),
                        )
        self.assertGreaterEqual(
            seen, 25, "no sorted header read -- the scan is vacuous"
        )

    def test_every_clickable_row_carries_an_id(self):
        seen = 0
        for name, html in self.rendered.items():
            if not isinstance(html, str):
                continue
            for table_id, body in TABLE_RE.findall(html):
                for row_id in ROW_ID_RE.findall(body):
                    seen += 1
                    with self.subTest(renderer=name, table=table_id):
                        self.assertTrue(
                            row_id,
                            "a row of %s exposes an empty data-id, so the "
                            "modal it opens has nothing to fetch" % table_id,
                        )
        self.assertGreaterEqual(seen, 5, "no clickable row read -- the scan is vacuous")

    def test_every_clickable_table_opens_something(self):
        """A table whose rows carry a `data-id` is a table the click handler is
        expected to open a modal on. The match is on the id suffix, in
        ROW_MODALS or in a fallback branch: an entry deleted leaves rows that
        look clickable and do nothing, and no renderer complains."""
        with open(APP_MJS, encoding="utf-8") as fh:
            text = fh.read()
        suffixes = ROW_MODAL_SUFFIX.findall(text) + FALLBACK_SUFFIX.findall(text)
        self.assertIn("apierr", suffixes, "the ROW_MODALS scan read nothing")
        self.assertIn("files", suffixes, "the fallback scan read nothing")
        for name, html in self.rendered.items():
            if not isinstance(html, str):
                continue
            for table_id, body in TABLE_RE.findall(html):
                if not ROW_ID_RE.findall(body):
                    continue
                with self.subTest(renderer=name, table=table_id):
                    self.assertTrue(
                        any(table_id.endswith(s) for s in suffixes),
                        "nothing in handleRowClick matches %s" % table_id,
                    )

    def test_the_subagents_tab_badge_counts_the_rows_the_tab_shows(self):
        """The badge and the table under it must count one population. The tab
        renders `subagents` (subagent_completed); a badge counting the
        delegation list would show, for a session that delegated more than it
        completed, a number no row backs."""
        seed_log_event(
            "tool_result",
            self.SID,
            [
                _attr("tool_name", _str("Task")),
                _attr("success", _str("true")),
                _attr("agent_type", _str("general-purpose")),
                _attr("tool_result_size_bytes", _str("100")),
            ],
        )
        payload = api_session(self.SID)
        # Counted from the database, since the payload ships no list. Without it
        # the assertion would pass on two equal counts.
        delegations = sum(
            t["calls"]
            for t in delegation_types(Scope(" AND session_id=?", (self.SID,)))
        )
        self.assertNotEqual(delegations, len(payload["subagents"]))
        html = render([("agents@session", payload)])["agents@session"]
        badge = re.search(r'data-v=agents class="[^"]*">Sub-agents<b>(\d+)</b>', html)
        self.assertIsNotNone(badge, "the Sub-agents tab badge was not rendered")
        self.assertEqual(int(badge.group(1)), len(payload["subagents"]))

    def test_the_costs_page_labels_its_token_types(self):
        """The legend and the stacked bar print a label, not the payload key. A
        rename that reached the DOM would ship `cache_read` to the reader, and no
        structural assertion would see it."""
        costs = self.rendered["costs"]
        self.assertIn('class="s-cache-read"', costs)
        self.assertIn("Cache read", costs)
        self.assertNotIn("s-cacheRead", costs)
        self.assertNotIn("cache_read", costs.replace("s-cache-read", ""))
        # The swatch of the same four types, on the session panel that draws them
        # from the palette rather than from the segment class.
        session = self.rendered["flow@session"]
        self.assertIn("var(--tok-cache-read)", session)
        self.assertNotIn("var(--cr)", session)

    def test_the_costs_page_reads_the_main_thread_off_the_payload(self):
        """The share outside the main thread is computed from the
        `is_main_thread` flag each origin carries, so a spelling the page has
        never heard of still counts as the main thread. Its bar carries the raw
        `src`: the labels are the page's, the list of names is not."""
        origins = [
            {"src": "future_main", "cost": 3.0, "calls": 1, "is_main_thread": True},
            {"src": "subagent", "cost": 1.0, "calls": 1, "is_main_thread": False},
        ]
        payload = dict(self.payloads["costs"], origins=origins)
        costs = render([("costs", payload)])["costs"]
        self.assertIn("(25%) outside the main thread", costs)

    def test_a_model_family_named_like_an_object_key_takes_the_neutral_colour(self):
        """The colour lookup is keyed on a model family, which comes off the
        wire. Keyed in an object literal, a family named after an inherited
        property would answer with the prototype's member instead of a colour,
        and that member reaches the `fill` of the cost chart and the
        `background` of its legend."""
        costs = self.rendered["costs"]
        self.assertIn("__proto__", costs, "the hostile family never reached the page")
        self.assertNotIn("[object Object]", costs)
        self.assertNotIn("function ", costs)
        self.assertIn("var(--other)", costs)

    def test_the_cost_chart_reads_a_family_a_day_does_not_carry_as_zero(self):
        """A series row is a plain object off the wire, and a family missing from
        that day has no own property on it. Read with `[]`, an inherited member
        answers instead of a number, and that is the whole Costs page not
        rendering.

        The chart itself needs two days to exist at all -- `stackedAreaChart`
        returns early below two points -- so the fixture seeds a second one."""
        costs = self.rendered["costs"]
        self.assertIn("<path class=area", costs, "the stacked chart was not drawn")
        self.assertNotIn("Not enough data points", costs)
        self.assertIn(
            "$0.000", costs, "a family absent from a day did not read as zero"
        )

    def test_the_session_panel_prints_the_output_style_and_the_effort(self):
        """Two rows fed by lists: an empty one renders a fallback, so a key the
        backend stopped emitting would print the fallback rather than break, and
        no structural assertion would see it."""
        session = self.rendered["flow@session"]
        self.assertIn("Output style", session)
        self.assertIn(">Concise<", session)
        self.assertIn("Effort", session)
        self.assertIn(">high<", session)
        # The wire value never reaches the DOM: the suffix is split off backend
        # side, and the panel prints the style name alone.
        self.assertNotIn("outputStyle", session)

    def test_the_header_line_names_the_terminal_and_the_cli_version(self):
        """Both are lists, and both ride the line under the session title. A
        session can span two terminals and two CLI versions, so each segment
        joins its values; the version says whose it is, since a bare number
        beside the date would read as the dashboard's own."""
        payload = api_session(self.SID)
        payload["head"]["terminals"] = ["ghostty", "phpstorm"]
        payload["head"]["versions"] = ["2.1.161", "2.1.232"]
        line = render([("sub@session", payload)])["sub@session"]
        self.assertIn("ghostty, phpstorm", line)
        self.assertIn("Claude Code 2.1.161, 2.1.232", line)
        self.assertIn(self.SID[:8], line)

    def test_an_unexported_terminal_drops_its_segment_from_the_header(self):
        """A segment with nothing in it leaves no separator behind: a session
        that exported neither would otherwise end the line on a dangling
        middle dot, which reads as a value that failed to print."""
        payload = api_session(self.SID)
        payload["head"]["terminals"] = []
        payload["head"]["versions"] = []
        line = render([("sub@session", payload)])["sub@session"]
        self.assertNotIn("Claude Code", line)
        self.assertFalse(line.strip().endswith("&middot;"), line)
        self.assertNotIn("&middot; &middot;", line)
        self.assertIn(self.SID[:8], line)
        for marker in DEFECT_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, line)

    def test_a_count_cell_never_reaches_the_dom_as_markup(self):
        """Counters are payload values like any other. `failures`,
        `compactions`, `decisions`, `batches` and the hook error counters are
        read off the wire and interpolated into a template; nothing on the
        backend guarantees a number, so nothing here may assume one.

        Two guards stand between the wire and the DOM, and the assertion is on
        the outcome rather than on either: `formatNumber` coerces a value that
        is not a number to `0`, and `escapeHtml` neutralises what is left. A
        cell interpolating the field bare has neither."""
        hostile = XSS % "count"
        analysis = self.payloads["analysis"]
        session = self.payloads["session"]
        sessions = self.payloads["sessions"]
        health = self.payloads["health"]
        hook = api_hook("PreToolUse")
        cases = {
            "tools@global": dict(
                analysis, tools=[dict(analysis["tools"][0], failures=hostile)]
            ),
            "files@session": dict(
                session, files=[dict(session["files"][0], failures=hostile)]
            ),
            "prompts@global": dict(
                analysis,
                prompts=[
                    dict(analysis["prompts"][0], failures=hostile, compactions=hostile)
                ],
            ),
            "misc:dec@global": dict(
                analysis,
                decisions=[dict(analysis["decisions"][0], decisions=hostile)],
            ),
            "sessions": dict(
                sessions,
                sessions=[dict(sessions["sessions"][0], compactions=hostile)],
            ),
            "health": dict(
                health,
                masked_mcp=hostile,
                unknown=hostile,
                notes=[dict(health["notes"][0], batches=hostile)],
                hooks=[dict(health["hooks"][0], err=hostile, block=hostile)],
            ),
            "modal:hook": dict(
                hook, fires=[dict(hook["fires"][0], err=hostile, block=hostile)]
            ),
        }
        for name, payload in cases.items():
            with self.subTest(renderer=name):
                self.assertNotIn("<img", render([(name, payload)])[name])

    def test_every_interpolated_payload_value_reaches_the_dom_as_text(self):
        """One hostile value per field, so a site that stopped escaping names
        itself in the failure. All of them come off the wire untouched:
        ingestion keeps the attributes raw, and DROP_ATTRS only removes.

        Both directions are asserted. A site that dropped the field altogether
        satisfies the first assertion and loses a value without saying so, which
        is what the escaped form being there is for."""
        session = api_session(self.SID)
        session["head"]["terminals"] = [XSS % "terminal"]
        session["head"]["versions"] = [XSS % "version"]
        analysis = self.payloads["analysis"]
        api_error = analysis["api_errors"][0]
        # The axis label prints `d.slice(5)`, so a day hostile only in the year
        # the slice drops proves nothing about what reaches the DOM. Two points,
        # since `stackedAreaChart` returns early below that and draws no axis.
        day = "2026-" + XSS % "day"
        cases = {
            ("sub@session", "terminal"): session,
            ("sub@session", "version"): session,
            ("misc:apierr@global", "message"): dict(
                analysis, api_errors=[dict(api_error, error=XSS % "message")]
            ),
            # `status_code` is the one numeric column shipped without a CAST: it
            # is whatever json_extract found in the blob.
            ("misc:apierr@global", "status"): dict(
                analysis, api_errors=[dict(api_error, status_code=XSS % "status")]
            ),
            ("costs", "day"): dict(
                self.payloads["costs"],
                series=[{"d": day + str(i), "Opus": 1.0} for i in range(2)],
                families=["Opus"],
            ),
            ("modal:event", "evid"): {"id": XSS % "evid", "name": "tool_result"},
        }
        for (name, field), payload in cases.items():
            with self.subTest(renderer=name, field=field):
                html = render([(name, payload)])[name]
                self.assertNotIn("<img", html)
                self.assertIn(
                    "&lt;img src=x id=%s" % field,
                    html,
                    "%s never reached the DOM" % field,
                )

    def test_a_stat_card_escapes_the_hint_it_is_handed(self):
        """`statCard` owns the escaping of its label, value and hint: they are
        payload-derived at nearly every one of its call sites, and leaving it to
        the callers means every one of them has to remember. The overview cards
        read `kpi.prompts` straight off the wire into a hint."""
        overview = self.payloads["overview"]
        payload = dict(overview, kpi=dict(overview["kpi"], prompts=XSS % "hint"))
        overview_html = render([("overview", payload)])["overview"]
        self.assertNotIn("<img", overview_html)
        # Escaped, not dropped: a card that stopped printing its hint would
        # satisfy `assertNotIn` and lose a figure without saying so.
        self.assertIn("&lt;img", overview_html)

    def test_a_stat_card_prints_the_markup_of_its_hint_tail_as_markup(self):
        """The other direction, and the reason `hintTail` exists at all: the
        delta tag and the `estTokens` span are markup a caller builds, and
        escaping them by excess of zeal produces valid HTML that shows the tag
        as text -- a silent break no assertion on the hint side would catch."""
        overview = self.payloads["overview"]
        # `deltaTag` needs a previous window to compare against, and the seeded
        # one covers everything, so `api_overview` leaves `prev` null.
        cases = {
            "overview": (
                dict(overview, prev={k: 1 for k in ("sessions", "tool_calls", "cost")}),
                '<span class="dl',
            ),
            "flow@session": (self.payloads["session"], '<span title="Estimated'),
        }
        for name, (payload, tag) in cases.items():
            with self.subTest(renderer=name):
                html = render([(name, payload)])[name]
                self.assertIn(tag, html)
                self.assertNotIn("&lt;span", html)

    def test_the_session_panel_falls_back_to_a_dash_on_both_empty_lists(self):
        """A session that set no style and one predating `effort` ship empty
        lists, and the panel has no chip to print. `-` is what the rows above it
        print for an unknown: naming a style there would state a fact the payload
        does not carry -- the list is just as empty on a session with no exported
        request at all."""
        payload = api_session(self.SID)
        payload["head"]["output_styles"] = []
        payload["head"]["efforts"] = []
        session = render([("flow@session", payload)])["flow@session"]
        for label in ("Output style", "Effort"):
            with self.subTest(row=label):
                row = re.search(
                    r"<span class=dim>%s</span>\s*<span class=num>(.*?)</span>" % label,
                    session,
                    re.S,
                )
                self.assertIsNotNone(row, "the %s row was not rendered" % label)
                self.assertEqual(row.group(1).strip(), "-")
        # An empty list must not render an empty chip either.
        self.assertNotIn("<span class=tag></span>", session)


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestTheMiscTabsSplitTheThreeLists(BaseDBTest):
    """Errors & Permissions is a strip of three tabs, and the middle one is new.

    Stacked, the three lists made the view a scroll and gave the API errors no
    place at all. Each is now a tab of its own, and the counts on the strip are
    what says which of them is worth opening.
    """

    SID = "session-misc"

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
                _attr("tool_parameters", _str('{"full_command":"make"}')),
            ],
        )
        seed_log_event(
            "tool_decision",
            self.SID,
            [
                _attr("tool_name", _str("Bash")),
                _attr("decision", _str("reject")),
                _attr("source", _str("user_permanent")),
            ],
        )
        seed_log_event(
            "api_error",
            self.SID,
            [
                _attr("status_code", _int(529)),
                _attr("error", _str("Overloaded")),
                _attr("attempt", _int(11)),
                _attr("duration_ms", _str("195118")),
                _attr("model", _str("claude-opus-5")),
                _attr("prompt.id", _str("P1")),
            ],
        )
        self.analysis = api_analysis(NO_FILTER)
        self.session = api_session(self.SID)

    def test_the_strip_lists_the_three_tabs_in_the_order_asked_for(self):
        html = render([("misc@global", self.analysis)])["misc@global"]
        self.assertEqual(
            re.findall(r"<button data-tab=gmisc data-v=(\w+)", html),
            ["errd", "apierr", "dec"],
        )
        # Nothing selected: Failures is what opens.
        self.assertIn("<table data-t=gerrd", html)

    def test_the_api_errors_tab_shows_its_own_table_alone(self):
        html = render([("misc:apierr@global", self.analysis)])["misc:apierr@global"]
        self.assertIn("<table data-t=gapierr", html)
        self.assertEqual(len(ROW_ID_RE.findall(html)), len(self.analysis["api_errors"]))
        self.assertNotIn("data-t=gerrd", html)
        self.assertNotIn("data-t=gdec", html)
        for marker in DEFECT_MARKERS:
            self.assertNotIn(marker, html)

    def test_the_session_scope_drops_the_origin_columns(self):
        html = render([("misc:apierr@session", self.session)])["misc:apierr@session"]
        body = dict(TABLE_RE.findall(html))["sapierr"]
        self.assertNotIn('<th data-k="project"', body)
        self.assertNotIn('<th data-k="session_id"', body)

    def test_an_exhausted_row_says_so_and_an_orphan_says_what_it_lacks(self):
        """The two kinds of incident share a table, so the Kind cell is the only
        place they are told apart. An orphan carries no status and no message,
        and the cell is where that is said rather than left to dashes."""
        row = self.analysis["api_errors"][0]
        payload = dict(
            self.analysis,
            api_errors=[
                dict(row, id=1, exhausted=True, name="api_error"),
                dict(row, id=2, exhausted=True, name="api_retries_exhausted"),
                dict(row, id=3, exhausted=False, name="api_error"),
            ],
        )
        body = dict(
            TABLE_RE.findall(
                render([("misc:apierr@global", payload)])["misc:apierr@global"]
            )
        )["gapierr"]
        cells = re.findall(r"<tr data-id=\"(\d+)\">(.*?)</tr>", body, re.S)
        by_id = {i: c for i, c in cells}
        self.assertIn("Retries exhausted", by_id["1"])
        self.assertIn("no error reported", by_id["2"])
        self.assertNotIn("no error reported", by_id["1"])
        self.assertNotIn("Retries exhausted", by_id["3"])

    def test_the_attempt_count_reaches_the_row(self):
        """The column that says whether the call was tried once or eleven times.
        Nothing else on the row carries it."""
        payload = dict(
            self.analysis,
            api_errors=[dict(self.analysis["api_errors"][0], id=1, attempts=11)],
        )
        body = dict(
            TABLE_RE.findall(
                render([("misc:apierr@global", payload)])["misc:apierr@global"]
            )
        )["gapierr"]
        self.assertIn(">11<", body)

    def test_the_other_tab_counts_the_three_lists_it_holds(self):
        html = render([("misc@session", self.session)])["misc@session"]
        badge = re.search(r'data-v=misc class="[^"]*">Other<b>(\d+)</b>', html)
        self.assertIsNotNone(badge, "the Other tab badge was not rendered")
        self.assertEqual(
            int(badge.group(1)),
            len(self.session["errors"])
            + len(self.session["api_errors"])
            + len(self.session["decisions"]),
        )


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestTheTimelineSummarisesItsRows(BaseDBTest):
    """Seven kinds of record whose detail cell says more than their own name.

    The row already says what kind of event it is in its left column, so a
    detail cell repeating it says nothing. What the record carries -- the modes
    a permission switch went between, the server an MCP connection reached, how
    long a hook took -- is what makes the row readable.
    """

    SID = "session-summary"
    # data-ev="<id>" ... <span class=d><detail></span>, one per timeline row.
    ROW_RE = re.compile(r'data-ev="(\d+)".*?<span class=d>(.*?)</span></div>', re.S)

    def setUp(self):
        super().setUp()
        seed_summarised_events(self.SID)
        self.payload = api_session(self.SID)
        self.html = render([("flow@session", self.payload)])["flow@session"]
        details = dict(self.ROW_RE.findall(self.html))
        self.detail = {}
        for event in self.payload["events"]:
            self.detail.setdefault(event["name"], []).append(details[str(event["id"])])

    def test_a_mode_switch_names_both_modes_and_what_triggered_it(self):
        cells = " ".join(self.detail["permission_mode_changed"])
        self.assertIn("plan", cells)
        self.assertIn("auto", cells)
        self.assertIn("shift_tab", cells)

    def test_a_mode_switch_without_a_trigger_prints_no_empty_bracket(self):
        cell = next(c for c in self.detail["permission_mode_changed"] if "default" in c)
        self.assertIn("auto", cell)
        self.assertNotIn("()", cell)

    def test_an_at_mention_is_not_a_failure(self):
        """An at-mention carries `success=false` on nearly every record, with
        nothing saying what failed. Read as a failed call, they painted the
        timeline red."""
        cell = self.detail["at_mention"][0]
        self.assertIn("file", cell)
        self.assertNotIn("✗", cell)
        self.assertNotIn("failure", cell)

    def test_a_slow_hook_stands_out_and_a_fast_one_does_not(self):
        slow = next(c for c in self.detail["hook_execution_complete"] if "Stop" in c)
        fast = next(
            c for c in self.detail["hook_execution_complete"] if "PreToolUse" in c
        )
        self.assertIn("amber", slow)
        self.assertNotIn("amber", fast)
        self.assertIn("52", fast)

    def test_a_failed_mcp_connection_is_marked_and_a_live_one_is_not(self):
        failed = next(
            c for c in self.detail["mcp_server_connection"] if "soloscrum" in c
        )
        live = next(c for c in self.detail["mcp_server_connection"] if "codegraph" in c)
        self.assertIn("ko", failed)
        self.assertNotIn("ko", live)
        self.assertIn("stdio", live)

    def test_an_internal_error_names_itself_without_a_dangling_separator(self):
        cell = self.detail["internal_error"][0].strip()
        self.assertIn("Error", cell)
        # `error_code` is never sent: the one record in the whole base carries
        # `error_name` alone, so a separator waiting for a second field would
        # hang there for good.
        self.assertNotIn("&middot;", cell)

    def test_a_housekeeping_record_still_says_nothing(self):
        """`retention_sweep` is the CLI cleaning up after itself. Its detail
        equals its name, which the row already prints, so the cell stays empty."""
        self.assertEqual(self.detail["retention_sweep"][0].strip(), "")

    def test_an_api_error_shows_the_status_and_what_it_said(self):
        cell = self.detail["api_error"][0]
        self.assertIn("529", cell)
        self.assertIn("Overloaded", cell)
        self.assertIn("ko", cell)

    def test_an_exhausted_chain_shows_what_it_spent(self):
        cell = self.detail["api_retries_exhausted"][0]
        self.assertIn("11 attempts", cell)
        # 195 164 ms read as a duration, not as a raw millisecond count.
        self.assertNotIn("195164", cell)
        self.assertIn("ko", cell)

    def test_a_registered_hook_still_prints_its_name(self):
        """The `hook_name` fallback of describeEvent. A fire carries a summary
        of its own, so `hook_registered` is the one record on the fallback --
        dead-looking code that is not dead."""
        self.assertIn("PostToolUse:Edit", self.detail["hook_registered"][0])

    def test_every_summarised_field_is_escaped(self):
        """One hostile value per field, so a cell that stopped escaping names
        itself in the failure. Every one of these comes off the wire untouched:
        ingestion keeps the attributes raw, and DROP_ATTRS only removes."""
        fields = {
            "permission_mode_changed": ("from_mode", "to_mode", "trigger_kind"),
            "mcp_server_connection": ("mcp_status", "mcp_name", "mcp_transport"),
            "hook_execution_complete": ("hook_name",),
            "api_error": ("status_code", "error_msg"),
            "internal_error": ("error_name",),
            "at_mention": ("mention_type",),
            "hook_registered": ("hook_name",),
        }
        payload = api_session(self.SID)
        seeded = set()
        for event in payload["events"]:
            for field in fields.get(event["name"], ()):
                event[field] = XSS % field
                seeded.add(field)
        self.assertEqual(
            seeded,
            {f for fs in fields.values() for f in fs},
            "a field was never seeded -- the scan is vacuous",
        )
        html = render([("flow@session", payload)])["flow@session"]
        self.assertNotIn("<img", html)
        for field in sorted(seeded):
            with self.subTest(field=field):
                self.assertIn(
                    "&lt;img src=x id=%s" % field,
                    html,
                    "%s never reached a cell" % field,
                )


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestTheCompactionCaptionCountsWhatIsOffTheCurve(BaseDBTest):
    """The curve starts at the compaction before last, so the compactions left
    off it are the ones before that one -- two fewer than the session has, not
    one. A count off by one announces a drop the curve is drawing."""

    SID = "session-caption"
    CAPTION_RE = re.compile(r"(\d+) earlier compactions? (?:is|are) off it")

    def setUp(self):
        super().setUp()
        seed_metric("claude_code.session.count", 1, self.SID, "claude-opus-4-8")
        self.payload = api_session(self.SID)

    def _caption(self, compaction_count):
        """The session detail rendered with `compaction_count` compactions.

        One request per compaction plus a last one, so every compaction sits
        inside the curve and the sparkline has two points to draw.
        """
        payload = dict(
            self.payload,
            compactions=[
                {"ts": 100 + i * 10, "trigger_kind": "auto"}
                for i in range(compaction_count)
            ],
            context=[
                {"ts": 95 + i * 10, "value": 50000} for i in range(compaction_count + 2)
            ],
        )
        return render([("flow@session", payload)])["flow@session"]

    def test_up_to_two_compactions_leave_nothing_off_the_curve(self):
        for count in (1, 2):
            with self.subTest(compactions=count):
                self.assertNotIn("off it", self._caption(count))

    def test_three_compactions_leave_the_first_one_off_the_curve(self):
        self.assertEqual(self.CAPTION_RE.findall(self._caption(3)), ["1"])

    def test_four_compactions_leave_two_off_the_curve_and_read_as_plural(self):
        html = self._caption(4)
        self.assertEqual(self.CAPTION_RE.findall(html), ["2"])
        self.assertIn("compactions are off it", html)


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestTheContextTableFallsBackToThePeak(BaseDBTest):
    """Auto compactions rank the table, and on a healthy install every session
    has zero of them. Without a second key the rows then keep whatever order
    they arrived in, and the ranking the view promises never happens."""

    # The session cell of each row, in row order. The rows carry no `data-id`:
    # a context row opens nothing, so the link to the session detail is what
    # names it.
    ROW_SESSION_RE = re.compile(r'data-goto="session/([^"]+)"')

    def _order(self, sessions):
        """The session ids of the context table, top row first."""
        payload = {
            "sessions": sessions,
            "auto_compactions": 0,
            "manual_compactions": 0,
            "pre_compaction_peak": 0,
        }
        html = render([("context@global", payload)])["context@global"]
        return self.ROW_SESSION_RE.findall(html)

    @staticmethod
    def _session(session_id, auto_comp, peak):
        return {
            "session_id": session_id,
            "project": "ccdash",
            "ts": 1700000000,
            "events": 10,
            "auto_comp": auto_comp,
            "man_comp": 0,
            "pre_compaction_peak": peak,
            "max_context": peak,
            "cost": 1.0,
            "tools": 4,
            "prompts": 2,
            "tools_per_prompt": 2.0,
        }

    def test_sessions_that_never_compacted_are_ranked_on_their_peak(self):
        order = self._order(
            [
                self._session("s-low", 0, 10000),
                self._session("s-high", 0, 90000),
                self._session("s-mid", 0, 50000),
            ]
        )
        self.assertEqual(order, ["s-high", "s-mid", "s-low"])

    def test_the_peak_only_breaks_a_tie_on_the_auto_compactions(self):
        order = self._order(
            [
                self._session("s-quiet", 0, 90000),
                self._session("s-hot", 2, 10000),
            ]
        )
        self.assertEqual(order, ["s-hot", "s-quiet"])


@unittest.skipIf(
    shutil.which("node") is None, "node is required to execute the renderers"
)
class TestAnUnknownSessionRendersAsNotFound(BaseDBTest):
    """The payload the endpoint answers for an id the database never saw, run
    through the renderer the router hands it to.

    The head aggregates it is built from come back as a row of NULLs, which the
    panels below print as a session that started at the epoch and did nothing."""

    def test_the_session_view_states_it_and_draws_none_of_the_panels(self):
        # The body the handler answers the raise with, which is what the router
        # hands the renderer.
        html = render([("flow@session", {"error": NOT_FOUND})])["flow@session"]
        self.assertIsInstance(html, str, "the renderer threw: %r" % (html,))
        self.assertIn("No session with this id", html)
        # Neither the timeline nor the stat cards the panels are made of: a view
        # drawn from an absent session is the defect, whatever it prints in them.
        self.assertNotIn("<div class=tl>", html)
        self.assertNotIn("data-tab", html)
        for marker in DEFECT_MARKERS:
            self.assertNotIn(marker, html)

    def test_a_session_the_metrics_missed_still_carries_the_span_of_its_events(self):
        # The head reads metric_points; a session that registered a hook and
        # stopped has none, and a NULL timestamp renders as January 1970.
        ts = 1_700_000_000
        with store.write() as db:
            db.execute(
                "INSERT INTO events (ts,name,session_id) VALUES (?,'hook_registered',?)",
                (ts, "sess-events-only"),
            )
        payload = api_session("sess-events-only")
        self.assertEqual(payload["head"]["started_at"], ts)
        self.assertEqual(payload["head"]["ended_at"], ts)
        rendered = render(
            [("flow@session", payload), ("sub@session", payload)],
        )
        for name, html in rendered.items():
            with self.subTest(renderer=name):
                self.assertIsInstance(html, str, "the renderer threw: %r" % (html,))
                self.assertNotIn("1970", html)
                for marker in DEFECT_MARKERS:
                    self.assertNotIn(marker, html)


if __name__ == "__main__":
    unittest.main()
