"""HTTP layer: the handler seen from the outside, through real requests.

The other test files call `api_*` and `ingest_*` directly; here we go through the
server, because the routing itself was covered by nothing.
A real `ThreadingHTTPServer` on port 0, stdlib only.

Run: python3 -m unittest discover -s tests
"""

import gzip
import http.client
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import zlib
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from base import BaseDBTest, remove_db_files

from ccdash import api, ingest, server
from ccdash.core import store
from ccdash.pages import overview


def ingest_notes():
    """The ingestion journal, in order: what Diagnostics will show."""
    return [
        dict(r) for r in store.query("SELECT kind, note FROM ingest_log ORDER BY id")
    ]


class HttpCase(BaseDBTest):
    """One server shared by the class, a temporary DB per test.

    The handler resolves the connection on every request, so the server can outlive
    the DB being recycled between two tests.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def get(self, path):
        return self.request("GET", path)

    def get_json(self, path):
        status, _, body = self.get(path)
        self.assertEqual(status, 200, "%s -> %d: %s" % (path, status, body[:200]))
        return json.loads(body)

    def post(self, path, payload, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        return self.request("POST", path, body, h)

    def get_as_host(self, path, host):
        """GET with the Host header spelled by the test, `None` for a request
        carrying none — which http.client fills in by itself otherwise."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
            if host is not None:
                c.putheader("Host", host)
            c.endheaders()
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()

    def post_chunked(self, path, chunks, headers=None, raw=None):
        """POST with Transfer-Encoding: chunked, which http.client only frames
        by itself for a body it built. `chunks` are the pieces to frame; `raw`
        sends a body stream as it stands, malformed framing included."""
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            c.putrequest("POST", path)
            c.putheader("Content-Type", "application/json")
            c.putheader("Transfer-Encoding", "chunked")
            for k, v in (headers or {}).items():
                c.putheader(k, v)
            c.endheaders()
            if raw is None:
                raw = (
                    b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in chunks)
                    + b"0\r\n\r\n"
                )
            c.send(raw)
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()


class TestGetRoutes(HttpCase):
    """The 15 GET routes answer, and the HTML is served. The list is every key of
    API_ROUTES: a route missing from it is a route no test ever reaches through
    the handler, and a typo in its lambda passes the whole suite green."""

    # Spelled out rather than read back from API_ROUTES, which a dropped path
    # would drop out of too. Only /api/session names a record that has to exist,
    # so it is the only route an empty database 404s.
    ROUTES = {
        "/api/overview": ("", 200),
        "/api/projects": ("", 200),
        "/api/sessions": ("", 200),
        "/api/session": ("?id=x", 404),
        "/api/analysis": ("", 200),
        "/api/event": ("?id=1", 200),
        "/api/subagent": ("?id=1", 200),
        "/api/costs": ("", 200),
        "/api/context": ("", 200),
        "/api/calls": ("?label=Bash", 200),
        "/api/filters": ("", 200),
        "/api/hook": ("?name=x", 200),
        "/api/prompt": ("?id=1", 200),
        "/api/health": ("", 200),
        "/health": ("", 200),
    }

    def test_the_route_table_holds_exactly_the_routes_covered(self):
        self.assertEqual(sorted(api.API_ROUTES), sorted(self.ROUTES))

    def test_every_route_answers_json_on_an_empty_db(self):
        for path, (params, expected) in self.ROUTES.items():
            with self.subTest(route=path):
                status, headers, body = self.get(path + params)
                self.assertEqual(status, expected, "%s: %s" % (path, body[:200]))
                self.assertEqual(headers["Content-Type"], "application/json")
                json.loads(body)

    def test_projects_is_served_both_on_its_own_and_inside_the_overview(self):
        # The Projects page fetches it directly, the Overview embeds a page of
        # the same list, and api_costs reuses it for its bar chart. One
        # aggregate, three callers -- they must not drift apart.
        status, _, body = self.get("/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body), json.loads(self.get("/api/overview")[2])["projects"]
        )

    def test_page_served_at_root_and_index(self):
        for path in ("/", "/index.html"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
            self.assertIn(b"<title>ccdash</title>", body)

    def test_every_asset_is_served_with_its_content_type(self):
        for path, (body, ctype) in server.ASSETS.items():
            with self.subTest(path=path):
                status, headers, served = self.get(path)
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], ctype)
                self.assertEqual(served.decode(), body)

    def test_no_path_traversal_under_assets(self):
        # The request path is only ever a dict key, so none of these is a
        # filesystem path: they are misses on the allowlist, not blocked attempts.
        for path in (
            "/assets/",
            "/assets/nope.mjs",
            "/assets/../ccdash.py",
            "/assets/%2e%2e/ccdash.py",
            "/assets//ccdash.css",
        ):
            with self.subTest(path=path):
                status, _, _ = self.get(path)
                self.assertEqual(status, 404)

    def test_unknown_route_is_404(self):
        status, _, body = self.get("/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body), {"error": "route"})

    def test_health_says_nothing_but_that_the_server_is_up(self):
        # The container healthcheck polls this. The diagnostics payload names
        # the database path, and a probe has no use for it.
        self.assertEqual(self.get_json("/health"), {"ok": True})

    def test_the_diagnostics_route_carries_the_database_path(self):
        self.assertIn("db", self.get_json("/api/health"))

    def test_every_response_carries_the_hardening_headers(self):
        # Including the 404: it carries a JSON body like the rest.
        for path in ("/", "/assets/ccdash.css", "/api/overview", "/api/nope"):
            with self.subTest(path=path):
                _, headers, _ = self.get(path)
                self.assertIn("no-store", headers["Cache-Control"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_a_non_numeric_id_gives_400(self):
        # A malformed id is a malformed request, not a server failure -- and not
        # an empty payload either, which would read as "no such record".
        for path in ("/api/event", "/api/subagent"):
            with self.subTest(route=path):
                status, _, body = self.get(path + "?id=abc")
                self.assertEqual(status, 400)
                self.assertEqual(json.loads(body), {"error": "bad request"})

    def test_missing_event_id_is_a_normal_answer(self):
        self.assertEqual(self.get_json("/api/event")["error"], "not found")

    def test_a_failing_route_answers_a_generic_500_and_logs_the_detail(self):
        # The body an unauthenticated reader gets must not name the exception,
        # the module or the query behind it. The detail belongs on stderr.
        original = overview.api_overview

        def boom(*a, **k):
            raise RuntimeError("secret internal detail")

        overview.api_overview = boom
        errors = io.StringIO()
        stderr = sys.stderr
        sys.stderr = errors
        try:
            # A query no other test uses, so the response cache cannot answer it.
            status, _, body = self.get("/api/overview?days=113")
        finally:
            sys.stderr = stderr
            overview.api_overview = original
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "server error"})
        self.assertNotIn("secret internal detail", body.decode())
        self.assertIn("secret internal detail", errors.getvalue())


class TestGetFilters(HttpCase):
    """Query parameters do make it all the way to the rendered scope."""

    def setUp(self):
        super().setUp()
        # Two hosts, two projects, and one event inside each window: the three
        # bounds 1 / 7 / 90 days must give three different counts, otherwise a
        # badly forwarded `days` would go unnoticed.
        now = int(time.time())
        rows = [
            (now - 3600, "tool_result", "s1", "hostA", "projA"),
            (now - 3600, "tool_result", "s1", "hostA", "projA"),
            (now - 3600, "tool_result", "s2", "hostB", "projB"),
            (now - 3 * 86400, "tool_result", "s4", "hostA", "projA"),
            (now - 40 * 86400, "tool_result", "s3", "hostA", "projA"),
        ]
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts,name,session_id,host,project) VALUES (?,?,?,?,?)",
                rows,
            )
            # The same rows as metric points: the counters the Overview reads
            # come from there, so the events alone would leave every figure at
            # zero.
            db.executemany(
                "INSERT INTO metric_points (ts,name,value,session_id,host,project) "
                "VALUES (?,?,1,?,?,?)",
                [
                    (ts, "claude_code.session.count", sid, host, project)
                    for ts, _, sid, host, project in rows
                ],
            )

    def calls(self, query):
        return self.get_json("/api/overview?" + query)["kpi"]["tool_calls"]

    def test_host_filter_narrows(self):
        self.assertEqual(self.calls("days=7"), 4)
        self.assertEqual(self.calls("days=7&host=hostA"), 3)
        self.assertEqual(self.calls("days=7&host=hostB"), 1)

    def test_project_filter_narrows(self):
        self.assertEqual(self.calls("days=7&project=projB"), 1)

    def test_days_window_applies(self):
        self.assertEqual(self.calls("days=1"), 3)
        self.assertEqual(self.calls("days=7"), 4)
        self.assertEqual(self.calls("days=90"), 5)
        self.assertEqual(self.calls("days=0"), 5)  # 0 = no time filter

    def test_non_numeric_days_falls_back_to_seven(self):
        self.assertEqual(self.calls("days=abc"), self.calls("days=7"))
        self.assertNotEqual(self.calls("days=abc"), self.calls("days=1"))
        self.assertNotEqual(self.calls("days=abc"), self.calls("days=90"))

    def test_filters_endpoint_lists_what_was_seen(self):
        f = self.get_json("/api/filters")
        self.assertEqual(sorted(f["hosts"]), ["hostA", "hostB"])
        self.assertEqual(sorted(f["projects"]), ["projA", "projB"])


class TestGetRouteArguments(HttpCase):
    """The routes taking a named argument do read it.

    These are the only ones where the router extracts something other than the
    shared filters: an `id` or a `session` lost on the way would make the response
    plausible but wrong, with no error.
    """

    def setUp(self):
        super().setUp()
        now = int(time.time())
        with store.write() as db:
            db.executemany(
                "INSERT INTO events (ts,name,session_id,label,tool_name) VALUES (?,?,?,'Bash','Bash')",
                [
                    (now - 60, "tool_result", "sess-one"),
                    (now - 60, "tool_result", "sess-one"),
                    (now - 60, "tool_result", "sess-two"),
                ],
            )
            db.executemany(
                "INSERT INTO metric_points (ts,name,value,session_id,host,project) VALUES (?,?,1,?,?,?)",
                [
                    (
                        now - 60,
                        "claude_code.session.count",
                        "sess-one",
                        "hostA",
                        "projA",
                    ),
                    (
                        now - 60,
                        "claude_code.session.count",
                        "sess-two",
                        "hostB",
                        "projB",
                    ),
                ],
            )
            db.executemany(
                "INSERT INTO events (ts,name,hook_name,hook_duration_ms) "
                "VALUES (?,'hook_execution_complete',?,?)",
                [
                    (now - 60, "my-hook", 12),
                    (now - 60, "other-hook", 34),
                ],
            )
            db.execute(
                "INSERT INTO events (ts,name,session_id) VALUES (?,'subagent_completed',?)",
                (now - 60, "sess-one"),
            )
            # The inspector reads the blob from the sibling table now (#180).
            db.execute(
                "INSERT INTO event_attrs (event_id,attrs) VALUES (last_insert_rowid(),?)",
                (json.dumps({"agent_type": "explorer", "total_tokens": 42}),),
            )

    def test_session_id_selects_that_session(self):
        self.assertEqual(
            self.get_json("/api/session?id=sess-one")["head"]["project"], "projA"
        )
        self.assertEqual(
            self.get_json("/api/session?id=sess-two")["head"]["project"], "projB"
        )

    def test_an_unknown_session_id_is_404(self):
        # The aggregates answer a row of NULLs for an id nothing was recorded
        # under, which reads on screen as a session with no duration and no
        # tokens. The status is what tells the view it is looking at nothing.
        for query in ("?id=no-such-session", "?id=", ""):
            with self.subTest(query=query):
                status, _, body = self.get("/api/session" + query)
                self.assertEqual(status, 404)
                self.assertEqual(json.loads(body), {"error": "not found"})

    def test_a_session_known_only_to_the_events_is_found(self):
        # Metric points are what the head aggregates read, but a session that
        # registered a hook and stopped has events and no metric point. It exists.
        with store.write() as db:
            db.execute(
                "INSERT INTO events (ts,name,session_id) VALUES (?,'hook_registered',?)",
                (int(time.time()) - 60, "sess-events-only"),
            )
        status, _, _ = self.get("/api/session?id=sess-events-only")
        self.assertEqual(status, 200)

    def test_calls_scope_follows_the_session_parameter(self):
        self.assertEqual(len(self.get_json("/api/calls?label=Bash")), 3)
        self.assertEqual(
            len(self.get_json("/api/calls?label=Bash&session=sess-one")), 2
        )
        self.assertEqual(
            len(self.get_json("/api/calls?label=Bash&session=sess-two")), 1
        )

    def test_detail_endpoints_return_the_requested_row(self):
        # The two inspectors: a badly forwarded id would show the detail of another
        # event, which is plausible on screen and therefore invisible.
        ids = [
            r["id"]
            for r in store.query(
                "SELECT id FROM events WHERE name='tool_result' ORDER BY id"
            )
        ]
        self.assertEqual(
            self.get_json("/api/event?id=%d" % ids[0])["session_id"], "sess-one"
        )
        self.assertEqual(
            self.get_json("/api/event?id=%d" % ids[2])["session_id"], "sess-two"
        )
        sub = store.query("SELECT id FROM events WHERE name='subagent_completed'")[0][
            "id"
        ]
        self.assertEqual(
            self.get_json("/api/subagent?id=%d" % sub)["agent_type"], "explorer"
        )
        self.assertEqual(
            self.get_json("/api/subagent?id=%d" % ids[0])["error"], "not found"
        )

    def test_hook_name_selects_that_hook(self):
        hook = self.get_json("/api/hook?name=my-hook")
        self.assertEqual(hook["name"], "my-hook")
        self.assertEqual([f["duration_ms"] for f in hook["fires"]], [12])
        self.assertEqual(
            self.get_json("/api/hook?name=other-hook")["fires"][0]["duration_ms"], 34
        )

    def test_a_duplicated_query_parameter_keeps_the_first_value(self):
        # `parse_qs` returns a list; _one takes the first. Arbitrary but pinned,
        # otherwise a ?days=1&days=90 would change meaning at the next refactor.
        self.assertEqual(
            self.get_json("/api/hook?name=my-hook&name=other-hook")["name"], "my-hook"
        )


class TestPostIngestion(HttpCase):
    """The POST dispatch, path by path."""

    METRICS = {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "session.id", "value": {"stringValue": "s-http"}}
                    ]
                },
                "scopeMetrics": [
                    {
                        "metrics": [
                            {
                                "name": "claude_code.cost.usage",
                                "sum": {
                                    "aggregationTemporality": 1,
                                    "dataPoints": [
                                        {
                                            "asDouble": 0.5,
                                            "timeUnixNano": "1700000000000000000",
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

    LOGS = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "session.id", "value": {"stringValue": "s-http"}}
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
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
                            }
                        ]
                    }
                ],
            }
        ]
    }

    def count(self, table):
        return store.query_value("SELECT COUNT(*) FROM " + table)

    def test_metrics_are_really_inserted(self):
        status, _, body = self.post("/v1/metrics", self.METRICS)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        self.assertEqual(self.count("metric_points"), 1)

    def test_logs_are_really_inserted(self):
        self.assertEqual(self.post("/v1/logs", self.LOGS)[0], 200)
        self.assertEqual(self.count("events"), 1)

    def test_a_post_carrying_an_origin_is_refused_and_stores_nothing(self):
        # The drive-by shape: a page you visit posting to 127.0.0.1 with your
        # own browser. The header is the tell, and the browser will not let
        # that page strip it.
        status, _, body = self.post(
            "/v1/logs", self.LOGS, {"Origin": "https://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "origin"})
        self.assertEqual(self.count("events"), 0)
        # The raw path, like every other rejection do_POST journals before it
        # knows which stream the request was aiming for.
        self.assertEqual(
            ingest_notes(), [{"kind": "/v1/logs", "note": "cross-origin POST refused"}]
        )

    def test_an_origin_of_null_is_refused_too(self):
        # What a sandboxed iframe and a file:// page send. Still a browser.
        self.assertEqual(self.post("/v1/logs", self.LOGS, {"Origin": "null"})[0], 403)
        self.assertEqual(self.count("events"), 0)

    def test_trailing_slash_and_prefix_still_dispatch(self):
        # The path is compared by suffix after a rstrip("/"): an exporter
        # configured with a prefixed base URL must keep working.
        self.assertEqual(self.post("/otlp/v1/logs/", self.LOGS)[0], 200)
        self.assertEqual(self.count("events"), 1)

    def test_traces_are_journalled_and_stored_nowhere(self):
        self.assertEqual(self.post("/v1/traces", {"resourceSpans": []})[0], 200)
        self.assertEqual(self.count("events"), 0)
        self.assertEqual(self.count("metric_points"), 0)
        self.assertEqual(ingest_notes(), [{"kind": "traces", "note": "traces ignored"}])

    def test_unknown_suffix_is_404_and_journalled(self):
        status, _, _ = self.post("/v1/whatever", self.LOGS)
        self.assertEqual(status, 404)
        self.assertEqual(
            ingest_notes(), [{"kind": "/v1/whatever", "note": "unknown route"}]
        )

    def test_empty_body_is_accepted_and_ingests_nothing(self):
        status, _, body = self.request("POST", "/v1/logs")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {})
        self.assertEqual(self.count("events"), 0)

    def test_a_compressed_body_is_ingested(self):
        codecs = (("gzip", gzip.compress), ("deflate", zlib.compress))
        for expected_rows, (encoding, compress) in enumerate(codecs, start=1):
            with self.subTest(encoding=encoding):
                raw = compress(json.dumps(self.LOGS).encode())
                status, _, _ = self.post(
                    "/v1/logs", None, {"Content-Encoding": encoding}, raw=raw
                )
                self.assertEqual(status, 200)
                self.assertEqual(self.count("events"), expected_rows)

    def test_undecodable_body_is_400_and_journalled(self):
        status, _, body = self.post(
            "/v1/logs", None, {"Content-Encoding": "gzip"}, raw=b"not gzip at all"
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "body"})
        self.assertTrue(ingest_notes()[0]["note"].startswith("body "))

    def test_protobuf_is_415_and_journalled(self):
        status, _, body = self.post(
            "/v1/metrics",
            None,
            {"Content-Type": "application/x-protobuf"},
            raw=b"\x0a\x02ok",
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body), {"error": "http/json required"})
        self.assertEqual(ingest_notes()[0]["note"], "protobuf received: use http/json")

    def test_an_empty_protobuf_body_is_still_415(self):
        # The Content-Type alone says the exporter is misconfigured; an empty
        # body must not turn that into a 200.
        status, _, body = self.post(
            "/v1/metrics", None, {"Content-Type": "application/x-protobuf"}, raw=b""
        )
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body), {"error": "http/json required"})

    def test_a_body_sent_without_a_content_type_is_ingested(self):
        # Only "protobuf" in that header turns a POST away. A header the
        # exporter left out says nothing about what it sent, so it must not
        # decide anything either.
        status, _, _ = self.request("POST", "/v1/logs", json.dumps(self.LOGS).encode())
        self.assertEqual(status, 200)
        self.assertEqual(self.count("events"), 1)

    def test_a_chunked_body_is_reassembled_across_chunks(self):
        # HTTP/1.1 lets an exporter stream a body whose length it does not know
        # yet; BaseHTTPRequestHandler reads none of it on its own.
        body = json.dumps(self.LOGS).encode()
        cut = len(body) // 3
        pieces = [body[:cut], body[cut : cut * 2], body[cut * 2 :]]
        status, _, _ = self.post_chunked("/v1/logs", pieces)
        self.assertEqual(status, 200)
        self.assertEqual(self.count("events"), 1)

    def test_a_chunked_gzip_body_is_ingested(self):
        # The two combine: framing is undone first, then the encoding.
        raw = gzip.compress(json.dumps(self.LOGS).encode())
        status, _, _ = self.post_chunked(
            "/v1/logs", [raw[:5], raw[5:]], {"Content-Encoding": "gzip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.count("events"), 1)

    def test_an_oversized_chunked_body_is_413_and_journalled(self):
        # No Content-Length announces the size, so the limit is enforced as the
        # chunks are read -- and must answer 413 like the announced case does.
        original = ingest.MAX_BODY
        ingest.MAX_BODY = 10
        try:
            status, _, body = self.post_chunked(
                "/v1/logs", [json.dumps(self.LOGS).encode()]
            )
        finally:
            ingest.MAX_BODY = original
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "too large"})
        self.assertTrue(ingest_notes()[0]["note"].startswith("body too large"))
        self.assertEqual(self.count("events"), 0)

    def test_an_unreadable_chunk_size_is_400_and_journalled(self):
        status, _, body = self.post_chunked(
            "/v1/logs", None, raw=b"zz\r\nnope\r\n0\r\n\r\n"
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "body"})
        self.assertTrue(ingest_notes()[0]["note"].startswith("body "))
        self.assertEqual(self.count("events"), 0)

    def test_invalid_json_is_400_and_journalled(self):
        status, _, body = self.post("/v1/metrics", None, raw=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "json"})
        self.assertEqual(ingest_notes()[0]["note"], "invalid json")

    def test_a_non_numeric_content_length_is_400_and_journalled(self):
        # The header is the client's, so an unreadable one is its mistake to
        # hear about -- not an exception raised out of the handler.
        status, _, body = self.post(
            "/v1/metrics", self.METRICS, {"Content-Length": "abc"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "length"})
        self.assertEqual(ingest_notes()[0]["note"], "unreadable Content-Length")
        self.assertEqual(self.count("metric_points"), 0)

    def test_a_negative_content_length_is_400(self):
        # `read(-1)` would take it as "until EOF" and read past every ceiling.
        status, _, body = self.post(
            "/v1/metrics", self.METRICS, {"Content-Length": "-1"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body), {"error": "length"})

    def test_oversized_body_is_413_and_journalled(self):
        original = ingest.MAX_BODY
        ingest.MAX_BODY = 10
        try:
            status, _, body = self.post("/v1/logs", self.LOGS)
        finally:
            ingest.MAX_BODY = original
        self.assertEqual(status, 413)
        self.assertEqual(json.loads(body), {"error": "too large"})
        self.assertTrue(ingest_notes()[0]["note"].startswith("body too large"))

    def test_ingester_failure_is_500_and_journalled(self):
        # We break `_walk`, called by both ingesters, rather than the ingester
        # itself: the test stays valid whatever the shape of the dispatch becomes.
        original = ingest._walk

        def boom(*a, **k):
            raise RuntimeError("nope")

        ingest._walk = boom
        errors = io.StringIO()
        stderr = sys.stderr
        sys.stderr = errors
        try:
            status, _, body = self.post("/v1/metrics", self.METRICS)
        finally:
            sys.stderr = stderr
            ingest._walk = original
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "ingest"})
        # The class name and nothing else: Diagnostics serves this journal over
        # /api/health, where an exception message would carry paths and payload.
        self.assertEqual(ingest_notes()[0]["note"], "ingest RuntimeError")
        self.assertIn("RuntimeError: nope", errors.getvalue())

    def test_a_successful_post_journals_the_batch(self):
        self.post("/v1/metrics", self.METRICS)
        self.assertEqual(ingest_notes(), [{"kind": "metrics", "note": None}])


class TestVerboseLogging(unittest.TestCase):
    def test_the_request_log_follows_the_verbose_flag(self):
        """The consumer side of `-v`: main publishes store.verbose, and this is the
        only code that reads it. log_message never touches the instance, so it runs
        without a live request."""
        captured = io.StringIO()
        verbose, stderr = store.verbose, sys.stderr
        sys.stderr = captured
        try:
            store.verbose = False
            server.Handler.log_message(None, "%s %s", "GET", "/api/overview")
            self.assertEqual(captured.getvalue(), "")
            store.verbose = True
            server.Handler.log_message(None, "%s %s", "GET", "/api/health")
        finally:
            sys.stderr, store.verbose = stderr, verbose
        self.assertEqual(captured.getvalue(), "  GET /api/health\n")


class TestMainOptions(BaseDBTest):
    def test_main_publishes_its_options_into_store(self):
        """`--db` and `-v` are read by the endpoints and by the request log, which
        live in other modules: keeping them local to ccdash breaks both silently.

        `--db` names a file the setUp did not publish, so the assertion reads what
        main wrote and not what was already in place.
        """

        class Server:
            def __init__(self, *a):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

        fd, main_db = tempfile.mkstemp(suffix=".db", prefix="ccdash_main_")
        os.close(fd)
        os.unlink(main_db)  # db_init creates the file
        self.addCleanup(remove_db_files, main_db)
        argv, saved, verbose = sys.argv, server.ThreadingHTTPServer, store.verbose
        sys.argv = ["ccdash", "--db", main_db, "--port", "0", "-v"]
        server.ThreadingHTTPServer = Server
        try:
            server.main()
            self.assertEqual(store.db_path, main_db)
            self.assertIs(store.verbose, True)
        finally:
            sys.argv, server.ThreadingHTTPServer, store.verbose = argv, saved, verbose


class TestHostHeader(HttpCase):
    """DNS rebinding: a page you visit resolves its own domain to the address
    ccdash listens on, and the browser then treats the two as one origin. The
    Origin refusal covers the write path only — this is what keeps the read path
    from answering. The header is the tell, and a browser will not let that page
    forge it."""

    LOGS = {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [{"timeUnixNano": "1700000000000000000"}]}]}
        ]
    }

    def test_a_default_name_is_answered(self):
        # The spellings a name reaches the allowlist in are TestHostName's, in
        # test_transport.py; one of them through a socket pins the path itself.
        for host in ("127.0.0.1", "127.0.0.1:%d" % self.port):
            with self.subTest(host=host):
                self.assertEqual(self.get_as_host("/health", host)[0], 200)

    def test_a_name_nobody_declared_is_refused(self):
        status, body = self.get_as_host("/api/overview", "evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "host"})

    def test_a_get_carrying_no_host_is_refused(self):
        self.assertEqual(self.get_as_host("/api/overview", None)[0], 403)

    def test_the_refusal_covers_the_page_and_the_assets_too(self):
        # Not only /api/: the shell is what fetches them, and serving it under a
        # rebound name is serving the attacker the code that reads the payloads.
        for path in ("/", "/assets/app.mjs"):
            with self.subTest(path=path):
                self.assertEqual(self.get_as_host(path, "evil.example")[0], 403)

    def test_a_declared_name_is_answered(self):
        original = server.allowed_hosts
        server.allowed_hosts = original | {"vm.lan"}
        try:
            self.assertEqual(self.get_as_host("/health", "vm.lan")[0], 200)
        finally:
            server.allowed_hosts = original

    def test_a_refusal_is_journalled_carrying_nothing_the_caller_sent(self):
        # The name and the path are both the caller's to choose, and Diagnostics
        # serves this journal: the note says what happened, never what was sent.
        server.host_refused_journalled = False
        self.get_as_host("/api/overview", "evil.example")
        self.assertEqual(
            ingest_notes(),
            [{"kind": server.HOST_REFUSED_KIND, "note": server.HOST_REFUSED}],
        )

    def test_a_repeated_refusal_writes_one_row_and_no_more(self):
        # The caller is unauthenticated and free to repeat. A row per refusal
        # would grow the database and hold the write lock the ingestion needs.
        server.host_refused_journalled = False
        for _ in range(5):
            self.get_as_host("/api/overview", "evil.example")
        self.assertEqual(len(ingest_notes()), 1)

    def test_an_exporter_posting_under_an_unknown_host_still_ingests(self):
        # The check is on GET alone. An exporter is not a browser, it cannot be
        # made to read anything back, and its base URL may name an alias nobody
        # declared.
        status, _, _ = self.request(
            "POST",
            "/v1/logs",
            json.dumps(self.LOGS).encode(),
            {"Content-Type": "application/json", "Host": "otel.internal"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(store.query_value("SELECT COUNT(*) FROM events"), 1)


if __name__ == "__main__":
    unittest.main()
