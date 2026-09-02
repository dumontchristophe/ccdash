"""Static assets and bounded decompression (inflate).

Run: python3 -m unittest discover -s tests
"""

import gzip
import os
import re
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdash import ingest, server

ASSET_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ccdash",
    "web",
    "assets",
)


class TestPage(unittest.TestCase):
    def test_theme_is_applied_before_the_module_loads(self):
        # app.mjs is a deferred module: a boot script placed after it would set
        # the theme only once the wrong one had already been painted.
        self.assertLess(
            server.PAGE.index("ccdash-theme"), server.PAGE.index('type="module"')
        )


class TestAssetAllowlist(unittest.TestCase):
    """The allowlist is the whole security model of /assets/, and the only way to
    forget a module is to leave it out of it — which shows up as a blank dashboard
    with a single 404 in the network tab. These tests are that missing signal."""

    def test_every_file_on_disk_is_listed(self):
        on_disk = sorted(n for n in os.listdir(ASSET_DIR) if not n.startswith("."))
        self.assertEqual(on_disk, sorted(server.ASSET_FILES))

    def test_every_url_referenced_by_the_shell_is_served(self):
        referenced = set(re.findall(r'["\'](/assets/[^"\']+)["\']', server.PAGE))
        self.assertTrue(referenced, "the shell references no asset at all")
        self.assertEqual(referenced - set(server.ASSETS), set())

    def test_every_import_resolves_to_a_served_module(self):
        # A module importing one that is not in the allowlist loads nothing at all:
        # the browser reports a single 404 and the dashboard stays blank.
        imports = set()
        for name in server.ASSET_FILES:
            if name.endswith(".mjs"):
                body = server.ASSETS["/assets/" + name][0]
                imports |= set(re.findall(r'from\s+"\./([^"]+)"', body))
        self.assertTrue(imports, "no module imports another one")
        self.assertEqual(imports - set(server.ASSET_FILES), set())


class TestHostName(unittest.TestCase):
    """A Host header reaches the allowlist as a bare name, whichever of the four
    shapes a client sent it in."""

    def test_a_bare_name_is_lowercased(self):
        self.assertEqual(server.host_name("LocalHost"), "localhost")

    def test_the_port_is_dropped(self):
        self.assertEqual(server.host_name("127.0.0.1:4318"), "127.0.0.1")

    def test_ipv6_keeps_its_colons_when_bare(self):
        self.assertEqual(server.host_name("::1"), "::1")

    def test_ipv6_loses_its_brackets_and_its_port(self):
        self.assertEqual(server.host_name("[::1]:4318"), "::1")
        self.assertEqual(server.host_name("[fe80::1]"), "fe80::1")

    def test_an_absent_header_is_a_name_nothing_allows(self):
        self.assertEqual(server.host_name(""), "")


class TestHostAllowlist(unittest.TestCase):
    def test_the_loopback_names_are_always_in(self):
        self.assertEqual(
            server.host_allowlist("127.0.0.1", []), {"127.0.0.1", "localhost", "::1"}
        )

    def test_the_bind_address_joins_them(self):
        self.assertIn("192.168.1.46", server.host_allowlist("192.168.1.46", []))

    def test_declared_names_join_them(self):
        allowed = server.host_allowlist("127.0.0.1", ["vm.lan", "CCDASH.local"])
        self.assertIn("vm.lan", allowed)
        self.assertIn("ccdash.local", allowed)

    def test_a_declared_name_may_carry_its_port(self):
        self.assertIn("vm.lan", server.host_allowlist("127.0.0.1", ["vm.lan:4318"]))

    def test_a_wildcard_bind_contributes_nothing(self):
        # It names every interface and no host: no client can send it back.
        for bind in ("0.0.0.0", "::", ""):
            with self.subTest(bind=bind):
                self.assertEqual(
                    server.host_allowlist(bind, []),
                    {"127.0.0.1", "localhost", "::1"},
                )

    def test_a_wildcard_declared_with_a_port_is_dropped_too(self):
        # Normalised before the wildcards are subtracted, so `0.0.0.0:4318` goes
        # the way the bare form does rather than slipping in beside it.
        self.assertEqual(
            server.host_allowlist("127.0.0.1", ["0.0.0.0:4318", "[::]:4318"]),
            {"127.0.0.1", "localhost", "::1"},
        )


class TestDeclaredHosts(unittest.TestCase):
    """CCDASH_ALLOW_HOST is how compose declares a name: the container always
    binds the wildcard, and compose cannot append a flag conditionally."""

    def setUp(self):
        self.original = os.environ.get("CCDASH_ALLOW_HOST")

    def tearDown(self):
        os.environ.pop("CCDASH_ALLOW_HOST", None)
        if self.original is not None:
            os.environ["CCDASH_ALLOW_HOST"] = self.original

    def declared(self, value):
        os.environ["CCDASH_ALLOW_HOST"] = value
        return server.declared_hosts()

    def test_unset_declares_nothing(self):
        os.environ.pop("CCDASH_ALLOW_HOST", None)
        self.assertEqual(server.declared_hosts(), [])

    def test_a_comma_separated_list_is_split_and_trimmed(self):
        self.assertEqual(
            self.declared(" vm.lan , 192.168.1.46 "), ["vm.lan", "192.168.1.46"]
        )

    def test_an_empty_value_declares_nothing(self):
        # The `${CCDASH_ALLOW_HOST:-}` default of compose, when nobody set one.
        self.assertEqual(self.declared(""), [])
        self.assertEqual(self.declared(",,"), [])


class TestInflate(unittest.TestCase):
    def test_roundtrip(self):
        payload = b'{"resourceLogs": []}' * 100
        for wbits, compress in ((31, gzip.compress), (15, zlib.compress)):
            with self.subTest(wbits=wbits):
                self.assertEqual(ingest.inflate(compress(payload), wbits), payload)

    def test_the_cap_passes_what_fits_and_rejects_a_bomb(self):
        # The bomb compresses tiny and expands well past the cap once we shrink
        # it; the body under the cap is what says the cap does not refuse alone.
        payload = b"a" * 512
        original = ingest.MAX_DECOMPRESSED
        ingest.MAX_DECOMPRESSED = 1024
        try:
            self.assertEqual(ingest.inflate(gzip.compress(payload), 31), payload)
            with self.assertRaises(ValueError):
                ingest.inflate(gzip.compress(b"a" * 1_000_000), 31)
        finally:
            ingest.MAX_DECOMPRESSED = original


if __name__ == "__main__":
    unittest.main()
