#!/usr/bin/env python3
"""
ccdash - local OTLP receiver + dashboard for Claude Code.

Python stdlib only. No dependency, no outbound network, and nothing to build:
the repo's one build step is the stylesheet, which this file only reads from disk.

    python3 -m ccdash            # http://127.0.0.1:4318/
"""

import argparse
import json
import os
import sys
import traceback
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import api, ingest
from .core import request, store

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# The served frontend lives inside the package, a direct child: no `..`.
WEB_DIR = os.path.join(APP_DIR, "web")


def _read(*parts: str) -> str:
    with open(os.path.join(WEB_DIR, *parts), encoding="utf-8") as fh:
        return fh.read()


PAGE = _read("index.html")

# Explicit rather than mimetypes.guess_type, which does not know .mjs on every
# platform, and a module served under the wrong type is refused outright.
ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
}

# An allowlist: a request path is only ever a key, never a filesystem path.
ASSET_FILES = [
    "ccdash.css",
    "state.mjs",
    "format.mjs",
    "charts.mjs",
    "components.mjs",
    "tables.mjs",
    "modals.mjs",
    "analysis.mjs",
    "pages.mjs",
    "app.mjs",
]

# Read at startup: no I/O per request, and a missing file fails the boot.
ASSETS = {
    "/assets/" + n: (_read("assets", n), ASSET_TYPES[os.path.splitext(n)[1]])
    for n in ASSET_FILES
}


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# "Every interface", so no client can send one back as a Host.
WILDCARD_BINDS = {"0.0.0.0", "::", ""}

# Neither the name nor the path is journalled: Diagnostics serves this, and
# both are the caller's to choose.
HOST_REFUSED_KIND = "http"
HOST_REFUSED = "cross-host GET refused"

# One row per process, not per refusal: an unauthenticated caller may repeat,
# and every row holds the write lock.
host_refused_journalled = False

allowed_hosts = set(LOOPBACK_HOSTS)


def host_name(header: str) -> str:
    """The bare name of a Host header: no port, no IPv6 brackets, lowercase.

    The port is dropped, not matched: a rebound name arrives on the same port a
    legitimate one does."""
    value = header.strip().lower()
    if value.startswith("["):
        return value[1:].partition("]")[0]
    # A bare IPv6 address is all colons and no port; a name and its port hold one.
    return value.partition(":")[0] if value.count(":") == 1 else value


def host_allowlist(bind: str, declared: Sequence[str]) -> set[str]:
    """The names a GET is answered under.

    The loopback ones, the bind address, and whatever the operator declared.
    Normalised before the wildcards are dropped, so `0.0.0.0:4318` goes the way
    `0.0.0.0` does.

    Args:
        bind: The address passed to --host.
        declared: The names passed to --allow-host and CCDASH_ALLOW_HOST.

    Returns:
        The bare lowercase names a Host header is matched against.
    """
    return LOOPBACK_HOSTS | ({host_name(n) for n in (bind, *declared)} - WILDCARD_BINDS)


def declared_hosts() -> list[str]:
    """The names `CCDASH_ALLOW_HOST` declares, comma-separated. It exists for
    compose, which cannot append a flag conditionally."""
    raw = os.environ.get("CCDASH_ALLOW_HOST", "")
    return [name.strip() for name in raw.split(",") if name.strip()]


# The detail of an unhandled failure goes to stderr, never into this body.
SERVER_ERROR = "server error"


def encode_body(payload: Any) -> bytes:
    """A route's answer as the bytes sent and cached. `default=str` covers the
    values SQLite hands back that JSON has no form for."""
    return json.dumps(payload, ensure_ascii=False, default=str).encode()


class Handler(BaseHTTPRequestHandler):
    server_version = "ccdash"

    def log_message(self, fmt: str, *args: Any) -> None:
        if store.verbose:
            sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(
        self,
        code: int,
        body: str | bytes,
        content_type: str = "application/json",
    ) -> None:
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(b)))
        # Without it the browser re-serves a stale API body on a tab switch.
        self.send_header("Cache-Control", "no-store, max-age=0")
        # The Content-Type is always this server's; sniffing can only fight it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(b)
        except BrokenPipeError:
            pass

    def _json(self, payload: Any) -> None:
        self._send(200, encode_body(payload))

    def _read_body(self, chunked: bool, length: int) -> bytes:
        """The body as bytes: frames reassembled, then the encoding undone.
        Raises rather than answers: the status is do_POST's to choose."""
        raw = (
            ingest.read_chunked(self.rfile, ingest.MAX_BODY)
            if chunked
            else self.rfile.read(length)
        )
        enc = self.headers.get("Content-Encoding", "").lower()
        if "gzip" in enc:
            return ingest.inflate(raw, 31)
        if "deflate" in enc:
            return ingest.inflate(raw, 15)
        return raw

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        # An exporter never sends Origin; a browser cannot remove it. Not
        # authentication: anything but a browser reaches the port anyway.
        if self.headers.get("Origin") is not None:
            ingest.log_ingest(path, 0, 0, "cross-origin POST refused")
            return self._send(403, '{"error":"origin"}')
        # Before the body: this is how the exporter is configured, not what
        # it sent.
        if "protobuf" in self.headers.get("Content-Type", "").lower():
            ingest.log_ingest(path, 0, 0, "protobuf received: use http/json")
            return self._send(415, '{"error":"http/json required"}')
        # A chunked body announces no length, so no Content-Length check can
        # see its size coming: read_chunked enforces MAX_BODY as it reads.
        chunked = "chunked" in self.headers.get("Transfer-Encoding", "").lower()
        # `read(-1)` means "until EOF", which reads past every ceiling.
        n = store.as_int(self.headers.get("Content-Length") or 0)
        if n is None or n < 0:
            ingest.log_ingest(path, 0, 0, "unreadable Content-Length")
            return self._send(400, '{"error":"length"}')
        if not chunked and n > ingest.MAX_BODY:
            ingest.log_ingest(path, 0, 0, "body too large (%d bytes)" % n)
            return self._send(413, '{"error":"too large"}')
        try:
            raw = self._read_body(chunked, n)
        except ingest.BodyTooLarge as ex:
            ingest.log_ingest(path, 0, 0, "body too large (%s bytes)" % ex)
            return self._send(413, '{"error":"too large"}')
        except Exception as ex:
            # The class name, not the repr: /api/health serves this journal.
            ingest.log_ingest(path, 0, 0, "body %s" % type(ex).__name__)
            return self._send(400, '{"error":"body"}')
        if not raw:
            return self._send(200, "{}")
        try:
            payload = json.loads(raw)
        except ValueError:
            ingest.log_ingest(path, 0, 0, "invalid json")
            return self._send(400, '{"error":"json"}')
        ingester = next(
            (fn for suffix, fn in ingest.INGESTERS if path.endswith(suffix)), None
        )
        if ingester is None:
            ingest.log_ingest(path, 0, 0, "unknown route")
            return self._send(404, '{"error":"route"}')
        try:
            ingester(payload)
        except Exception as ex:
            # The class name, not the repr: /api/health serves this journal.
            ingest.log_ingest(path, 0, 0, "ingest %s" % type(ex).__name__)
            sys.stderr.write("ccdash: %s ingest\n%s" % (path, traceback.format_exc()))
            return self._send(500, '{"error":"ingest"}')
        return self._send(200, "{}")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        p, params = u.path, parse_qs(u.query)
        # Before the page and the payloads alike. No Host reaches this as the
        # empty name, which nothing allows.
        if host_name(self.headers.get("Host", "")) not in allowed_hosts:
            global host_refused_journalled
            if not host_refused_journalled:
                host_refused_journalled = True
                ingest.log_ingest(HOST_REFUSED_KIND, 0, 0, HOST_REFUSED)
            self.log_message("host %r refused on %s", self.headers.get("Host"), p)
            return self._send(403, '{"error":"host"}')
        days = store.as_int(request.one_param(params, "days", "7"))
        filters = request.Filters(
            days=7 if days is None else days,
            host=request.one_param(params, "host") or None,
            project=request.one_param(params, "project") or None,
        )
        try:
            if p in ("/", "/index.html"):
                return self._send(200, PAGE, "text/html; charset=utf-8")
            asset = ASSETS.get(p)
            if asset is not None:
                return self._send(200, asset[0], asset[1])
            handler = api.API_ROUTES.get(p)
            if handler is not None:
                return self._send(200, encode_body(handler(params, filters)))
        # A record that is not there is an answer, not a server failure.
        except request.NotFoundError:
            return self._send(404, json.dumps({"error": request.NOT_FOUND}))
        except request.BadRequestError:
            return self._send(400, json.dumps({"error": request.BAD_REQUEST}))
        except Exception:
            # The reader gets a status, the operator the traceback: a body
            # would hand out module names, paths and SQL.
            sys.stderr.write("ccdash: %s failed\n%s" % (p, traceback.format_exc()))
            return self._send(500, json.dumps({"error": SERVER_ERROR}))
        return self._send(404, '{"error":"route"}')


def main() -> None:
    global allowed_hosts
    ap = argparse.ArgumentParser(description="OTLP receiver + Claude Code dashboard")
    ap.add_argument("--port", type=int, default=4318)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=os.path.expanduser("~/.ccdash/ccdash.db"))
    ap.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="NAME",
        help="another name a GET may arrive under, repeatable",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    declared = a.allow_host + declared_hosts()
    allowed_hosts = host_allowlist(a.host, declared)
    if a.host in WILDCARD_BINDS:
        # It names no host a browser can send, so the operator would otherwise
        # meet a 403 with no explanation.
        print(
            "ccdash: --host %s names no host a browser can send; anything but "
            "%s goes in --allow-host" % (a.host, ", ".join(sorted(LOOPBACK_HOSTS)))
        )
    store.db_path, store.verbose = a.db, a.verbose
    store.db_init(a.db)
    print("ccdash  http://%s:%d/   db=%s" % (a.host, a.port, a.db))
    print("        hosts=%s" % ", ".join(sorted(allowed_hosts)))
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
