// Executes a named frontend function under node and hands what it returns back
// to tests/test_render.py, which owns the payloads and the assertions.
//
// Running it is the point: a stale column key, a spread on a renamed parameter
// or a bar fed the wrong field are all valid JavaScript and all invisible to a
// source scan. They only exist once the template has actually run.
//
//   stdin  [{ "name": <job>, "data": <payload> }, ...]
//   stdout { "<job>": <whatever the job returned> | { "error": "<message>" } }
import { readFileSync } from "node:fs";
import { formatDuration } from "../ccdash/web/assets/format.mjs";
import { pages, sessionSubtitle } from "../ccdash/web/assets/pages.mjs";
import { analysisTabs, GLOBAL, SESSION } from "../ccdash/web/assets/analysis.mjs";
import { hookDetail, promptDetail, callsModal, detailView } from "../ccdash/web/assets/modals.mjs";
import { tab } from "../ccdash/web/assets/state.mjs";

// The one browser global the modals read: `callsModal` drops the origin columns
// when the drill-down was opened from a session detail. Nothing else in the
// rendered modules touches `document`, `window` or `location`.
globalThis.location = { hash: "" };

// `pages.session` picks its tab from the shared state rather than an argument,
// so a session-scoped renderer has to set it before rendering.
const sessionTab = (view) => (data) => {
  tab.sess = view;
  return pages.session(data);
};

// The misc view carries a strip of its own, namespaced by scope, and reads it
// from the same shared state. `sub` null renders whatever the default is, and
// clears what an earlier job in this batch may have left there.
const miscTab = (scope, sub) => (data) => {
  tab[scope + "misc"] = sub;
  return scope === GLOBAL ? analysisTabs.get("misc")(data, GLOBAL) : sessionTab("misc")(data);
};

const RENDERERS = new Map([
  ["overview", (data) => pages.overview(data)],
  ["projects", (data) => pages.projects(data)],
  ["sessions", (data) => pages.sessions(data)],
  ["costs", (data) => pages.costs(data)],
  ["health", (data) => pages.health(data)],
  ["tools@global", (data) => analysisTabs.get("tools")(data, GLOBAL)],
  ["bash@global", (data) => analysisTabs.get("bash")(data, GLOBAL)],
  ["prompts@global", (data) => analysisTabs.get("prompts")(data, GLOBAL)],
  ["agents@global", (data) => analysisTabs.get("agents")(data, GLOBAL)],
  ["misc@global", miscTab(GLOBAL, null)],
  ["misc:errd@global", miscTab(GLOBAL, "errd")],
  ["misc:apierr@global", miscTab(GLOBAL, "apierr")],
  ["misc:dec@global", miscTab(GLOBAL, "dec")],
  ["context@global", (data) => analysisTabs.get("context")(data, GLOBAL)],
  // Not a page: the header line app.mjs prints under the session title. The
  // router itself cannot be imported here -- it touches the DOM at load -- so
  // the line is built in pages.mjs and rendered on its own.
  ["sub@session", (data) => sessionSubtitle(data.head, data.session)],
  ["flow@session", sessionTab("flow")],
  ["files@session", sessionTab("files")],
  ["tools@session", sessionTab("tools")],
  ["bash@session", sessionTab("bash")],
  ["prompts@session", sessionTab("prompts")],
  ["agents@session", sessionTab("agents")],
  ["misc@session", miscTab(SESSION, null)],
  ["misc:apierr@session", miscTab(SESSION, "apierr")],
  ["misc:dec@session", miscTab(SESSION, "dec")],
  // Modals. Not pages either: app.mjs opens them on a click and feeds them one
  // endpoint each, so the harness calls them the way the router does.
  ["modal:hook", (data) => hookDetail(data)],
  ["modal:prompt", (data) => promptDetail(data)],
  ["modal:calls", (data) => callsModal(data.label, data.calls)],
  ["modal:event", (data) => detailView(data)],
  // A formatter rather than a renderer, run over a list of seconds so one job
  // covers every branch.
  ["format:duration", (seconds) => seconds.map(formatDuration)],
]);

// Null prototype: a job named `__proto__` has to land as an own property, not
// call the setter of Object.prototype and vanish from the JSON.
const out = Object.create(null);
for (const job of JSON.parse(readFileSync(0, "utf8"))) {
  const render = RENDERERS.get(job.name);
  if (!render) {
    out[job.name] = { error: `no renderer named ${job.name}` };
    continue;
  }
  try {
    out[job.name] = render(job.data);
  } catch (e) {
    out[job.name] = { error: `${e.constructor.name}: ${e.message}` };
  }
}
process.stdout.write(JSON.stringify(out));
