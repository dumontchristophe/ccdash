"""The stylesheet is a build artefact, and these tests are what keeps it honest.

`styles/input.css` is the file a human edits; `ccdash/web/assets/ccdash.css` is
what the binary produces from it and what the server sends. Nothing forces the
two to agree, so these tests read the committed output and demand that it match
the markup it is supposed to have been built from.

Run: python3 -m unittest discover -s tests
"""

import glob
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdash import server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILT = os.path.join(ROOT, "ccdash", "web", "assets", "ccdash.css")

# A class attribute, quoted or not — the modules write both forms.
CLASS_ATTR = re.compile(r"""class=(?:"([^"]*)"|'([^']*)'|([^\s>"'`]+))""")
RESPONSIVE = re.compile(r"^-?(?:max-)?(?:md|lg):")


def _layer_body(css, name):
    """The text between the braces of a top-level `@layer <name> { … }` block."""
    opening = css.index("@layer %s {" % name)
    depth, start = 0, css.index("{", opening)
    for index in range(start, len(css)):
        if css[index] == "{":
            depth += 1
        elif css[index] == "}":
            depth -= 1
            if depth == 0:
                return css[start + 1 : index]
    raise AssertionError("@layer %s is not balanced" % name)


def _class_names(block):
    return set(re.findall(r"\.([A-Za-z_][A-Za-z0-9_-]*)(?![\w\\-])", block))


class TestBuild(unittest.TestCase):
    def setUp(self):
        with open(BUILT, encoding="utf-8") as handle:
            self.built = handle.read()

    def test_no_generated_utility_shadows_a_component_class(self):
        """`@layer utilities` outranks `@layer components`, so a shared name wins
        by precedence and silently restyles the authored rule. Scanning the
        modules as text produces candidates that were never meant as classes
        (`grid`, `table`, `block` scraped out of inline styles), which is exactly
        how such a collision would arrive."""
        utilities = _class_names(_layer_body(self.built, "utilities"))
        components = _class_names(_layer_body(self.built, "components"))
        self.assertTrue(components, "the components layer holds no class rule at all")
        self.assertEqual(utilities & components, set())

    def test_every_responsive_candidate_is_in_the_built_css(self):
        """A `@source` that matches nothing is a silent success: the binary exits 0
        and prints `Done`, and the responsive utilities simply cease to exist while
        the dashboard stays perfectly styled. Nothing else in the suite would see
        it, so this walks the markup and demands a selector for every breakpoint
        token it finds."""
        candidates = set()
        for path in [os.path.join(ROOT, "ccdash", "web", "index.html")] + sorted(
            glob.glob(os.path.join(ROOT, "ccdash", "web", "assets", "*.mjs"))
        ):
            with open(path, encoding="utf-8") as handle:
                for match in CLASS_ATTR.finditer(handle.read()):
                    value = next(group for group in match.groups() if group is not None)
                    candidates.update(
                        token for token in value.split() if RESPONSIVE.match(token)
                    )
        self.assertTrue(
            candidates, "no responsive utility is used anywhere in the markup"
        )
        missing = sorted(
            token
            for token in candidates
            if ("." + re.sub(r"([^A-Za-z0-9_-])", r"\\\1", token)) not in self.built
        )
        self.assertEqual(
            missing,
            [],
            "utilities used in the markup are absent from the build -- rebuild the stylesheet",
        )

    def test_the_served_stylesheet_is_a_build_output(self):
        body = server.ASSETS["/assets/ccdash.css"][0]
        self.assertTrue(
            body.startswith("/*! tailwindcss v4"),
            "the served stylesheet is not a Tailwind build output",
        )


class TestScopeDiscipline(unittest.TestCase):
    def test_no_read_module_renders_a_scope_by_hand(self):
        """A read module never composes a window itself.

        A windowed query goes through `aggregates.scoped`/`windowed`, which
        aligns the args to the markers. A module reaching for `scope.clause` or
        `scope.args` is reintroducing the hand-composed `+ scope.clause` / `args
        * N` this seam removed -- the very slip that left `health` unbounded. The
        renderers themselves live in `aggregates.py`, the one exemption."""
        offenders = []
        pkg = os.path.join(ROOT, "ccdash")
        for path in sorted(glob.glob(os.path.join(pkg, "**", "*.py"), recursive=True)):
            if os.path.basename(path) == "aggregates.py":
                continue
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if "scope.clause" in text or "scope.args" in text:
                offenders.append(os.path.basename(path))
        self.assertEqual(
            offenders,
            [],
            "compose the window through aggregates.scoped/windowed, not by hand",
        )


class TestImage(unittest.TestCase):
    def test_the_image_copies_the_whole_package(self):
        """The image copies the `ccdash/` package wholesale, so a new module ships
        with no `COPY` edit. This guards against a regression to per-file copies,
        where a module left off the list builds an image that dies on import."""
        with open(os.path.join(ROOT, "Dockerfile"), encoding="utf-8") as handle:
            dockerfile = handle.read()
        copied_files = [
            token
            for line in dockerfile.splitlines()
            if line.startswith("COPY ")
            for token in re.findall(r"[\w./]+\.py", line)
        ]
        self.assertEqual(
            copied_files, [], "COPY names .py files instead of the package"
        )
        self.assertIn("COPY ccdash/", dockerfile)
