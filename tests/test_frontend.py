# Run: python3 -m unittest discover -s tests -v  (from repo root)
import os
import re
import unittest

APP = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ccdash", "web"
)
# The shell plus every module: the SVG builders live in the assets, and a scan
# of index.html alone would be vacuously green.
SOURCES = [os.path.join(APP, "index.html")] + sorted(
    os.path.join(APP, "assets", n)
    for n in os.listdir(os.path.join(APP, "assets"))
    if n.endswith(".mjs")
)

# An unquoted attribute value ends at a space or at '>', never at '/', so
# `attr=value/>` folds the slash in and is not self-closing: in an SVG every
# sibling after it becomes a child of that shape and stops being rendered.
UNQUOTED_BEFORE_SLASH = re.compile(r"""[a-zA-Z-]+=[^"'\s>][^\s>]*/>""")

# `<span class="tag ${...}">` renders model families, skill kinds and event types,
# all three straight from an attribute the sender chose. The class attribute is the
# dangerous half: a double quote closes it and the rest is parsed as markup.
TAG_ATTR = re.compile(r'class="tag \$\{([^{}]*)\}"')
TAG_TEXT = re.compile(r'class="tag [^"]*">\$\{([^{}]*)\}')


class TestTagEscaping(unittest.TestCase):
    """Every value interpolated into a tag span is escaped, with no exception.
    A few of them are provably safe -- picked from a fixed list rather than
    read from the payload -- and are escaped anyway: a rule that admits
    exceptions cannot be checked by this test, and stops being followed.

    Not subsumed by the broad interpolation scan: that one only reports a
    chained read (`x.family`), and a bare `map` parameter (`${m}`) falls
    outside it -- this shape is the only net over those sites."""

    def test_every_tag_span_escapes_what_it_interpolates(self):
        offenders = []
        for path in SOURCES:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for pattern in (TAG_ATTR, TAG_TEXT):
                offenders += [
                    "%s: %s" % (os.path.basename(path), expr.strip())
                    for expr in pattern.findall(text)
                    if "escapeHtml(" not in expr
                ]
        self.assertEqual(offenders, [], "escape these:\n" + "\n".join(offenders))


# The formatters live in format.mjs and every other module interpolates them.
FORMAT_MJS = os.path.join(APP, "assets", "format.mjs")
RENDERERS = [p for p in SOURCES if p.endswith(".mjs") and p != FORMAT_MJS]

# `formatNumber(`, `formatBytes(`, ... -- and not estTokens, the one formatter
# returning markup, which escaping would print as text.
FORMATTER_CALL = re.compile(r"\b(format[A-Z]\w*)\s*\(")
# The identifier a `(` belongs to, read off the text right before it.
CALLEE = re.compile(r"([A-Za-z_$][\w$]*)\s*$")
# statCard escapes the label, the value and the hint it is handed, so a
# formatter sitting in one of its arguments is already covered.
ESCAPING_CALLERS = ("escapeHtml", "statCard")


def scan_literals(text):
    """The blanked source, and the spans of the interpolations that reach markup.

    Every string, template chunk and comment is blanked to spaces, so the
    parentheses left standing are the ones the code opens. Offsets are
    preserved, and the code inside a template's `${ }` is kept: that is where
    the interpolations being checked live.

    The spans are the code inside each `${ }` of a template whose static text
    holds a `<`. A template without one builds a URL, where
    `encodeURIComponent` is the rule, or a phrase handed to a renderer that
    escapes what it is given -- `statCard` escapes its hint, so a hint escaping
    itself would print `&amp;` where the payload had `&`.

    A regular expression literal is read as ordinary code, and one holding a
    quote would open a literal that never closes.

    Returns:
        The blanked source and the (start, end) pairs, in closing order.

    Raises:
        ValueError: on an unterminated literal, rather than a file blanked from
            that point on, which would scan clean and prove nothing.
    """
    out = list(text)
    # "'" '"' for a literal, "}" for a block, ["`", static text, spans] for a
    # template and ["{", start] for one of its interpolations.
    stack, markup = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        top = stack[-1] if stack else None
        kind = top[0] if isinstance(top, list) else top
        if kind in ("'", '"', "`"):
            if c == "\\":
                out[i] = out[min(i + 1, n - 1)] = " "
                i += 2
            elif c == kind:
                stack.pop()
                if kind == "`" and "<" in top[1]:
                    markup += top[2]
                out[i] = " "
                i += 1
            elif kind == "`" and c == "$" and text[i + 1 : i + 2] == "{":
                stack.append(["{", i + 2])
                out[i] = out[i + 1] = " "
                i += 2
            else:
                if kind == "`":
                    top[1] += c
                out[i] = " "
                i += 1
            continue
        if c == "/" and text[i + 1 : i + 2] == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
        elif c == "/" and text[i + 1 : i + 2] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
        else:
            end = None
        if end is not None:
            out[i:end] = " " * (end - i)
            i = end
            continue
        if c == "`":
            stack.append(["`", "", []])
            out[i] = " "
        elif c in "'\"":
            stack.append(c)
            out[i] = " "
        elif c == "{":
            stack.append("}")
        elif c == "}" and stack:
            top = stack.pop()
            if isinstance(top, list):  # end of a ${ }, back inside the template
                out[i] = " "
                for frame in reversed(stack):
                    if isinstance(frame, list):
                        frame[2].append((top[1], i))
                        break
        i += 1
    if stack:
        raise ValueError("unterminated %r -- the scan cannot be trusted" % stack[-1])
    return "".join(out), markup


def enclosing_callee(code, pos):
    """The function whose argument list `pos` sits directly in, or "" when the
    innermost open parenthesis is a grouping one."""
    depth = 0
    for i in range(pos - 1, -1, -1):
        if code[i] == ")":
            depth += 1
        elif code[i] == "(":
            if depth:
                depth -= 1
                continue
            name = CALLEE.search(code[:i])
            return name.group(1) if name else ""
    return ""


class TestFormatterOutputIsEscaped(unittest.TestCase):
    """A formatter returns text, and every renderer drops that text straight
    into markup. The output of formatNumber is digits today, so escaping it
    changes nothing on any payload that exists -- which is exactly why the two
    conventions coexisted for so long: `formatNumber(row.batches)` on one page
    and `escapeHtml(formatNumber(row.batches))` on the next, same field. One
    rule with no exceptions is what a reviewer can check and what a new call
    site follows; per-site judgement of "safe" is the habit this replaces."""

    def test_every_formatter_call_is_wrapped_or_handed_to_statcard(self):
        offenders = []
        for path in RENDERERS:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            name = os.path.basename(path)
            code, _ = scan_literals(text)
            # Per file, not over the sweep: a module swallowed whole by a literal
            # the scanner mis-read reads as formatter-free, and the 130 calls in
            # the other five would keep a sweep-wide count green.
            if FORMATTER_CALL.search(text):
                self.assertTrue(
                    FORMATTER_CALL.search(code),
                    "%s: every formatter call was blanked -- the scan is vacuous"
                    % name,
                )
            for m in FORMATTER_CALL.finditer(code):
                if enclosing_callee(code, m.start()) in ESCAPING_CALLERS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                offenders.append("%s:%d: %s" % (name, line, m.group(1)))
        self.assertEqual(offenders, [], "escape these:\n" + "\n".join(offenders))


# A chain of reads and calls: `d.regs.map(...).join("")`, `DAY_LABELS[d]`,
# `row.share.toFixed(1)`. Nested code is blanked before this runs, so a call's
# arguments and a subscript are already `()` and `[]` by the time it matches.
INTERPOLATED_READ = re.compile(
    r"[A-Za-z_$][\w$]*(?:\s*\??\.\s*[A-Za-z_$][\w$]*|\s*\([^()]*\)|\s*\[[^\[\]]*\])+"
)
# The method a chain ends on, `join` in `rows.join("")`.
CHAIN_TAIL = re.compile(r"\.([A-Za-z_$][\w$]*)(?:\(\))?$")

# The two allowlists, and the whole of them: an entry claims a value cannot carry
# markup, which is the judgement this scan exists to stop anyone making per site.
# A chain ending on one of these needs no escapeHtml.
SAFE_TAILS = {
    "join": "the fragments joined were each built by a template scanned here",
    "toFixed": "Number.prototype, and digits are all it returns",
}
# ... and these return markup, which escaping would print as text:
MARKUP_CHAINS = {
    "col.cell": "a column's cell renderer",
    "MODAL_VIEWS.get": "the modal dispatch of app.mjs",
}


def printed_reads(expr):
    """The property reads an interpolation prints, as (chain, offset) pairs.

    Everything nested inside a bracket is blanked first: an argument is the
    callee's business, and it is the callee that has to escape it. So is the
    test of a top-level ternary -- `d.model ? ... : ""` reads the field to pick
    a branch, not to print it."""
    out, depth = list(expr), 0
    for i, c in enumerate(expr):
        if c in "([{":
            depth += 1
            if depth > 1:
                out[i] = " "
        elif c in ")]}":
            depth -= 1
            if depth > 0:
                out[i] = " "
        elif depth:
            out[i] = " "
    code = "".join(out)
    test = code.find("?")
    while test != -1 and code[test : test + 2] in ("?.", "??"):
        test = code.find("?", test + 2)
    if test != -1:
        code = " " * (test + 1) + code[test + 1 :]
    reads = []
    for m in INTERPOLATED_READ.finditer(code):
        chain = re.sub(r"\s+", "", m.group(0))
        if "." in chain or "[" in chain:
            reads.append((chain, m.start()))
    return reads


def unescaped_reads(text):
    """Every read a module's markup prints without escaping it, as
    (line, chain) pairs, plus the number of interpolations the scan looked at."""
    code, markup = scan_literals(text)
    offenders, scanned = [], 0
    for start, end in markup:
        scanned += 1
        for chain, offset in printed_reads(code[start:end]):
            tail = CHAIN_TAIL.search(chain)
            if tail and tail.group(1) in SAFE_TAILS:
                continue
            if re.split(r"[(\[]", chain)[0] in MARKUP_CHAINS:
                continue
            offenders.append((text.count("\n", 0, start + offset) + 1, chain))
    return offenders, scanned


class TestMarkupInterpolationsAreEscaped(unittest.TestCase):
    """A payload value at a call site is a read off an object -- `e.id`,
    `d.compaction_count`, `s.d.slice(5)` -- so a read printed by markup with
    nothing escaping it is what the scan reports. Reading for the rule is what
    lets a site through; this is the part a reader does not have to do.

    It claims no more than that. What a call is handed is that call's business,
    a value put in a local before being interpolated falls outside, and so does
    a template holding no markup -- `statCard` escapes the phrases it is given
    and escaping them twice would print `&amp;` where the payload had `&`.
    Proving the property in general needs the views rendered against a hostile
    payload, which tests/test_render.py does."""

    def test_no_module_prints_an_unescaped_read(self):
        offenders, scanned = [], 0
        for path in RENDERERS:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            reads, seen = unescaped_reads(text)
            scanned += seen
            offenders += [
                "%s:%d: %s" % (os.path.basename(path), line, chain)
                for line, chain in reads
            ]
        self.assertGreater(scanned, 300, "the interpolation scan came up empty")
        self.assertEqual(offenders, [], "escape these:\n" + "\n".join(offenders))

    def test_the_scan_reports_a_site_of_the_shape_it_is_for(self):
        # An id bare beside an escaped name: the module scan reads green either
        # way, so the reporting is what has to be held to a case that must fail.
        defect = """const inspector = (e) => `<p class=cap>Event ${e.id}
          &middot; ${escapeHtml(e.name)}</p>`;"""
        self.assertEqual(unescaped_reads(defect)[0], [(1, "e.id")])
        self.assertEqual(
            unescaped_reads(defect.replace("e.id", "escapeHtml(e.id)"))[0], []
        )


# The shape bytesCell carries. The two fields are not held equal: a copy reading
# the size off one column and the token line off another is the defect this
# reports. `row.` is what makes it a cell rather than a flow line of pages.mjs.
BYTES_CELL = re.compile(r"formatBytes\(\s*row\.\w+\s*\)[^`]*?estTokens\(\s*row\.\w+")


class TestOneWayToRenderABytesCell(unittest.TestCase):
    """A result size is a byte figure over an estimated token count, and the
    two must be read off the same field. Spelled out per column, every copy is
    a site the escaping can be dropped from and a site the second read can name
    the wrong field; the shape has a name in components.mjs."""

    def test_no_view_module_spells_the_bytes_cell_itself(self):
        offenders = []
        for path in RENDERERS:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            offenders += [
                "%s:%d" % (os.path.basename(path), text.count("\n", 0, m.start()) + 1)
                for m in BYTES_CELL.finditer(text)
            ]
        self.assertEqual(
            offenders, [], "hand these to bytesCell:\n" + "\n".join(offenders)
        )


APP_MJS = os.path.join(APP, "assets", "app.mjs")

# `const ROUTES = new Map([ [ "key", { ... } ], ... ]);` -- the key of each entry
# is the only quoted string sitting alone on the line right after a `[`.
ROUTE_KEY = re.compile(r'^\s*\[\n\s*"([A-Za-z0-9_-]+)",', re.M)
# `["Analysis", ["context", "tools", "misc"]],` -- the inner list of each group.
NAV_GROUP_LIST = re.compile(r'^\s*\["[A-Za-z]*",\s*\[([^\]]*)\]', re.M)


class TestNavMatchesRoutes(unittest.TestCase):
    """A key listed in NAV_GROUPS with no ROUTES entry throws on `r.icon` while
    the sidebar is being built, so the whole menu renders empty -- not one
    missing line, nothing at all. That is the failure mode of half-removing a
    view, and nothing else in the suite reads either constant."""

    def test_nav_groups_and_routes_declare_the_same_keys(self):
        with open(APP_MJS, encoding="utf-8") as fh:
            text = fh.read()
        routes = set(ROUTE_KEY.findall(text))
        nav = {
            key
            for group in NAV_GROUP_LIST.findall(text)
            for key in re.findall(r'"([A-Za-z0-9_-]+)"', group)
        }
        self.assertTrue(routes, "no ROUTES key matched -- the scan is vacuous")
        self.assertEqual(routes, nav)


ANALYSIS_MJS = os.path.join(APP, "assets", "analysis.mjs")
PAGES_MJS = os.path.join(APP, "assets", "pages.mjs")

# `["tools", (d, scope) =>` over two lines -- one entry of the analysisTabs Map.
# Scanned inside ANALYSIS_TABS only: `miscBodies` is a Map of the same shape, and
# a tab living there alone would otherwise read as an analysis tab.
ANALYSIS_TABS = re.compile(r"const analysisTabs = new Map\(\[(.*?)^\]\);", re.S | re.M)
TAB_KEY = re.compile(r'^    "([A-Za-z0-9_$]+)",\n    \(d, scope\) =>', re.M)
# `renderTabs("sess", view, [ ["flow", "Timeline", n], ... ])` in pages.session.
SESSION_TABS = re.compile(r'renderTabs\("sess", view, \[(.*?)\]\)', re.S)


class TestSessionTabsHaveARenderer(unittest.TestCase):
    """Every tab of the session detail but "flow" and "files" is rendered by
    `analysisTabs.get(view)(data, SESSION)`. A key with no renderer throws there and
    the tab body stays empty. bash, prompts and agents have no global view any
    more and only live here -- they read as dead code, and they are not."""

    def test_every_session_tab_but_flow_and_files_is_an_analysis_tab(self):
        with open(ANALYSIS_MJS, encoding="utf-8") as fh:
            table = ANALYSIS_TABS.search(fh.read())
        self.assertIsNotNone(table, "analysisTabs not found -- the scan is vacuous")
        renderers = set(TAB_KEY.findall(table.group(1)))
        self.assertTrue(
            renderers, "no analysisTabs renderer matched -- the scan is vacuous"
        )
        with open(PAGES_MJS, encoding="utf-8") as fh:
            call = SESSION_TABS.search(fh.read())
        self.assertIsNotNone(
            call, 'renderTabs("sess", ...) not found -- the scan is vacuous'
        )
        tabs = set(re.findall(r'\["([A-Za-z0-9_-]+)",', call.group(1))) - {
            "flow",
            "files",
        }
        self.assertTrue(tabs, "no session tab matched -- the scan is vacuous")
        self.assertEqual(tabs - renderers, set())


# `?id=${...}` / `&session=${...}` -- a value carried by a query parameter. The
# endpoint literals themselves are hand-written, so a placeholder is the only
# place an id from the payload reaches a URL.
QUERY_VALUE = re.compile(r"[?&]\w+=\$\{([^{}]*)\}")


class TestQueryValuesAreEncoded(unittest.TestCase):
    """Event ids come from the OTEL export and nothing in the ingest path
    constrains them to URL-safe characters: a `&` or a `#` in one silently
    truncates the request or splits it into a second parameter. One convention
    -- a template literal whose every value goes through encodeURIComponent --
    is what keeps an unencoded id from reading as normal code."""

    def test_every_query_parameter_value_goes_through_encodeuricomponent(self):
        with open(APP_MJS, encoding="utf-8") as fh:
            text = fh.read()
        values = QUERY_VALUE.findall(text)
        self.assertGreater(
            len(values), 0, "no query value matched -- the scan is vacuous"
        )
        offenders = [v for v in values if not v.startswith("encodeURIComponent(")]
        self.assertEqual(
            offenders, [], "encode these query values:\n" + "\n".join(offenders)
        )


class TestSvgSelfClosing(unittest.TestCase):
    """The charts are built as SVG strings; only the browser's HTML parser
    validates them. This guards the one mistake it does not report."""

    def test_no_unquoted_attribute_before_a_self_closing_slash(self):
        offenders = []
        for path in SOURCES:
            with open(path, encoding="utf-8") as fh:
                offenders += [
                    "%s:%d: %s" % (os.path.basename(path), n, line.strip())
                    for n, line in enumerate(fh, 1)
                    if UNQUOTED_BEFORE_SLASH.search(line)
                ]
        self.assertEqual(
            offenders, [], "quote these attribute values:\n" + "\n".join(offenders)
        )


# `active_seconds` states its unit; formatDuration() (format.mjs) takes seconds.
# A `/ 1000` on the way in would mean one of the two is lying.
ACTIVE_SECONDS_READ = re.compile(r"[\w.]*\bactive_seconds\b(\s*/\s*\d+)?")


class TestActiveSecondsReachesTheSecondsFormatter(unittest.TestCase):
    """The payload names the unit, so the front must consume it in that unit."""

    def test_every_active_seconds_read_goes_straight_to_formatduration(self):
        reads = 0
        for path in SOURCES:
            with open(path, encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    code = line.strip()
                    if "active_seconds" not in code or code.startswith("//"):
                        continue
                    for m in ACTIVE_SECONDS_READ.finditer(code):
                        reads += 1
                        where = "%s:%d" % (os.path.basename(path), n)
                        self.assertIsNone(
                            m.group(1), "%s divides a value already in seconds" % where
                        )
                        self.assertIn(
                            "formatDuration(%s)" % m.group(0),
                            code,
                            "%s: %s is not handed to formatDuration as it is"
                            % (where, m.group(0)),
                        )
        self.assertGreaterEqual(
            reads, 3, "no active_seconds read -- the scan is vacuous"
        )


if __name__ == "__main__":
    unittest.main()
