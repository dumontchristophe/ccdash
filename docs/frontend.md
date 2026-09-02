# Frontend — `ccdash/web/index.html` and `ccdash/web/assets/`

Native ES modules, served as written. No bundler, no transpiler, no runtime
dependency. The one build step is the stylesheet, compiled by the standalone
`tailwindcss` binary at development time — see [Editing](#editing).

## Files

`ccdash/web/index.html` is a 53-line shell: the stylesheet link, a boot script that
sets the theme before the first paint, the `#menu` button and `#scrim` backdrop
of the drawer, the `<aside>` sidebar whose `<nav id="nav">` `app.mjs` fills, an
empty `<main id="main">`, and the module entry point. `#menu` and `#scrim` sit
outside `#main` because `route()` rewrites `#main` wholesale on every render.

| Module | Contains |
|---|---|
| `state.mjs` | `sort`, `tab`, `pager`, `page` — plain objects, mutated by property, never reassigned |
| `format.mjs` | `qs`, the formatters, `escapeHtml`, `MODEL_COLORS`, `TOKEN_TYPES` |
| `charts.mjs` | the inline-SVG generators |
| `components.mjs` | `renderTable`, `paginate`, `renderTabs`, `statCard`, `numCell`, `bytesCell`, the shared columns (`whenCol`, `originCols`) |
| `tables.mjs` | one column-definition set per table |
| `modals.mjs` | the detail views |
| `analysis.mjs` | `analysisTabs`, the six analysis views |
| `pages.mjs` | `pages.*`, one renderer per top-level view, plus `sessionSubtitle` and the session-detail sections |
| `app.mjs` | `ROUTES`, the router, the delegated listeners |
| `ccdash.css` | every rule and both palettes — **generated**, see `styles/input.css` |

Imports only ever point down this list, so the graph is acyclic. Tables never
call a modal; that direction is carried by event delegation on `document`.

## Routing

The hash is the address. `ROUTES` in `app.mjs` is a `Map` from a route key to
its `{label, icon, sub, endpoint, render}` — the sidebar, header and fetch all
read from it. `NAV_GROUPS` groups those keys in the menu.

Eight top-level routes, plus `#/session/<id>`, which takes an argument and so is
handled outside `ROUTES`. Two of the eight — `#/tools` and `#/misc` — share
`/api/analysis` and refetch it on every switch; against a local SQLite file the
cost is invisible, and every view stays reloadable and shareable.

`#/session/<id>` is the one route parameter a reader can type, so it is the one
fetch that treats `404` as a payload: `fetchJson(url, 404)` returns the body,
and `pages.session` renders a "session not found" panel instead. Every other
`404` is an error banner. Its header subtitle is the one built from a payload
rather than written in `ROUTES` — `sessionSubtitle` builds it in `pages.mjs`,
where a test can run it, since `app.mjs` cannot be imported outside a browser.

`renderTabs` draws sub-tabs inside a single view: the session detail, and the
three tabs of Errors & Permissions (Failures, API errors, Permissions). The
latter renders under both scopes and namespaces its tab group by scope —
`gmisc` globally, `smisc` in a session — so the two strips do not share a
selection. In a session they nest, the inner strip inside the body of the outer.

## Rendering

A renderer takes a payload and returns an HTML string; `route()` writes it into
`<main>`. No virtual DOM, no incremental update: a refresh re-renders the page.
Two delegated listeners on `document` (`click`, `change`) handle the whole UI,
since per-element handlers would not survive the wholesale rewrite.

**Every value taken from a payload goes through `escapeHtml`**, with no
exception for the ones that look safe. The payload is attacker-influenced — a
model name, a Bash command, a prompt originate outside this machine — and is
rendered in the same origin that can read `/api/prompt`. `escapeHtml` covers
`&`, `<`, `>`, `"` and `'`, so an attribute is safe in either kind of quote.
Formatting and escaping live in `components.mjs` (`numCell`, `bytesCell`,
`statCard` escape their own inputs), so a column names only its field.

CI holds the rule, not a reader. `TestMarkupInterpolationsAreEscaped` in
`tests/test_frontend.py` flags every `${ }` in a markup template that prints a
property read unescaped; two short allowlists (`SAFE_TAILS`, `MARKUP_CHAINS`)
say what may go unescaped. `TestQueryValuesAreEncoded` holds `encodeURIComponent`
on URLs. Static regex cannot do taint analysis, so `tests/test_render.py`
renders the views against a hostile payload to prove the property.

**A lookup keyed by a payload value uses a `Map`, not an object literal.** An
event named `constructor` or `toString` would resolve against `Object.prototype`
on a literal and put a function in the DOM. `ROUTES`, `EVENT_DETAILS`,
`ORIGIN_NAMES`, `TEMPORALITY_NAMES`, `MODEL_COLORS`, `EXHAUSTED_LABELS`,
`TRUNCATED_LABELS` and `miscBodies` are `Map`s for that reason. `TOKEN_TYPES`,
`MODAL_VIEWS` and `analysisTabs` are `Map`s too, though their keys are written
in the modules. `MODEL_COLORS` shows the cost: `short_model` capitalises past
`constructor` but not `__proto__`, which on a literal reaches a `fill` as
`[object Object]`.

`pages.mjs` declares no column: a page renderer names a table renderer from
`tables.mjs` and hands it rows — `ingestTable(ingest)`. The analysis tables take
their id from the caller (drawn under both scopes, so the sorts must not share);
the six diagnostics tables carry their own. `modals.mjs` and `analysis.mjs`
still declare columns inline via `renderTable`.

## Modals

An open detail is a frame `{k, d, label}` in the `modals` stack `app.mjs` keeps
beside the route: `k` picks the renderer in `MODAL_VIEWS`, `d` is its payload,
`label` is the title the calls modal takes from what was clicked. `openModal`
fetches and pushes; `renderOpenModal` renders the top frame alone over the body.

A stack rather than one field per kind, because a drill-down chains — the calls
that touched a file, one of those calls, the prompt behind it — and the same
kind can recur in a chain. `Escape` and the close button pop one frame; a
backdrop click and any navigation clear the stack whole.

## Paging

`paginate(id, items, size = 50)` slices a list and returns `{visible, control}`,
reading and writing the page in `pager[id]`. `renderTable` calls it after
sorting the full array, so a header ranks the whole list, not the visible slice.
Three callers reach it directly for non-table lists: `projectRows`
(`PROJECTS_PER_PAGE`, 5), `pages.sessions` (default 50), `sessionTimeline` (100).

Two resets keep the stored index meaningful: a filter change clears every
`pager` entry (every list changes underneath), and sorting a table clears that
table's entry (re-ordering moves the rows). An index past the end of a shortened
list is clamped, not reset.

## Themes

`styles/input.css` carries two palettes: `:root` for dark and
`:root[data-theme="light"]` for light, which only redefines what the dark ramp
cannot serve inverted. They stay in plain unlayered `:root` blocks (not
`@theme`), so a Tailwind utility reaches them as `bg-(--card)` and modules keep
writing `var(--token)`.

**No literal colour exists outside those two blocks** — an SVG fill or a bar is
written `var(--token)`, which makes a palette change a one-file edit.

The boot script in `index.html` sets the theme from `localStorage`, falling back
to the system preference, before the first paint — which a deferred module could
not do. The `#theme` button swaps it with no re-render (a stylesheet swap draws
identical markup).

## Layout and the drawer

`@theme static` clears Tailwind's stock breakpoints and declares two:
`md: 768px` and `lg: 1000px`. A utility written against any other width compiles
to nothing.

The shared column layouts collapse below `lg`. Their media query uses
`grid-template-columns: minmax(0, 1fr)`, not `1fr`: a bare `1fr` is
`minmax(auto, 1fr)`, whose min-content floor is the widest child — the tab
strip, 574px — and that floor overrides any inner `overflow-x: auto`, pushing
the page sideways instead of scrolling inside it.

Below `md` the `<aside>` leaves the flow and becomes a drawer over the page:

- `#menu` toggles `documentElement.dataset.menu`. Pure CSS state, so no
  `reload()` — re-rendering would redraw identical markup for a transform.
- `#scrim` is the backdrop, present only while the drawer is open; a click on it
  is a click outside.
- `handleNavigation`, `hashchange` and a `matchMedia("(min-width: 768px)")`
  listener all close the drawer.
- `syncDrawer()` mirrors state into ARIA: `aria-expanded` on `#menu`, and
  `inert` on the `<aside>` while the drawer is shut below `md`, so the eight
  links do not stay focusable off-screen. It also runs once at boot.

After rewriting `#main`, `route()` restores each tab strip's `scrollLeft` onto
its active button.

Tables hide low-priority columns through `renderTable`: a column with
`hide: "max-md"` gets `max-md:hidden` on its header and cells. A hidden header
cannot be clicked, so a table's default sort (`first`, or its second column)
must name a visible column — `test_the_default_sort_never_names_a_hidden_column`
reads the rendered headers of the 29 tables the harness draws to hold it.

`renderTable` takes an optional `tiebreak` after `first`: a second key, compared
in the active direction, when the first ties. Opt-in because a tie is
meaningless on most tables but decisive on the Context table, which ranks on
auto compactions (0 on a healthy install) and falls back to `pre_compaction_peak`. The tiebreak
names a column, so a column hidden below 768px is a legitimate choice.

## Editing

```bash
tailwindcss -i styles/input.css -o ccdash/web/assets/ccdash.css  # after any class= edit
npx prettier --write ccdash/web/index.html ccdash/web/assets/* styles/*  # .prettierrc, width 100
node --check ccdash/web/assets/<module>.mjs                             # syntax, per module
```

`ccdash/web/assets/ccdash.css` is a build output, never edited by hand (see
CONTRIBUTING's *Rebuilding the stylesheet* for the binary and version floor;
`.prettierignore` keeps the formatter off it). A `class` attribute is build
input, so a new utility exists only once the stylesheet is rebuilt.

Assets are read once when the server starts: **restart it after any edit**, or
you will be looking at the previous version.
