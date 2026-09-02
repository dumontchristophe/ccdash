import { cache, page as pageState, pager, sort, tab } from "./state.mjs";
import { escapeHtml, qs } from "./format.mjs";
import {
  detailView,
  subagentDetail,
  hookDetail,
  promptDetail,
  callsModal,
  compactionsModal,
} from "./modals.mjs";
import { analysisTabs, GLOBAL } from "./analysis.mjs";
import { pages, sessionSubtitle } from "./pages.mjs";

// The shell loaded by index.html: the route table, the hash router and the
// delegated click handlers behind sorting, paging, tabs and modals.

// Every top-level route, in menu order. A Map and not an object literal: the key
// comes from the URL hash, and "#/constructor" would resolve to Object.prototype.
// Two routes share /api/analysis whole, which is the point: one server-side cache
// entry serves both, so whichever is opened first pays the query. The endpoint's
// `only` parameter would narrow it, at one cold query per switch.
const ROUTES = new Map([
  [
    "overview",
    {
      label: "Summary",
      icon: "\u25A6",
      sub: "Your Claude Code usage at a glance",
      endpoint: "/api/overview",
      render: (d) => pages.overview(d),
    },
  ],
  [
    "projects",
    {
      label: "Projects",
      icon: "\u25F0",
      sub: "Everything grouped by project",
      endpoint: "/api/projects",
      render: (d) => pages.projects(d),
    },
  ],
  [
    "sessions",
    {
      label: "Sessions",
      icon: "\u25AD",
      sub: "Session history",
      endpoint: "/api/sessions",
      render: (d) => pages.sessions(d),
    },
  ],
  [
    "tools",
    {
      label: "Tools",
      icon: "\u2692",
      sub: "Tool calls, skills and MCP servers",
      endpoint: "/api/analysis",
      render: (d) => analysisTabs.get("tools")(d, GLOBAL),
    },
  ],
  [
    "context",
    {
      label: "Context",
      icon: "◔",
      sub: "Sessions whose context filled up",
      endpoint: "/api/context",
      render: (d) => analysisTabs.get("context")(d, GLOBAL),
    },
  ],
  [
    "misc",
    {
      label: "Errors & Permissions",
      icon: "\u00B7",
      sub: "Failed calls, API errors and permission decisions",
      endpoint: "/api/analysis",
      render: (d) => analysisTabs.get("misc")(d, GLOBAL),
    },
  ],
  [
    "costs",
    {
      label: "Costs",
      icon: "\u25CE",
      sub: "Estimated cost and token weight",
      endpoint: "/api/costs",
      render: (d) => pages.costs(d),
    },
  ],
  [
    "health",
    {
      label: "Diagnostics",
      icon: "\u2699",
      sub: "Collection status",
      endpoint: "/api/health",
      render: (d) => pages.health(d),
    },
  ],
]);

// `modals` is a stack, oldest first, and only its last entry renders.
let filters = { days: 7, host: "", project: "" },
  modals = [],
  timer = null,
  lastFetch = 0,
  tick = null;

// [group heading, route keys]. An empty heading renders none: the everyday
// destinations sit above the grouping rather than in a category of their own.
const NAV_GROUPS = [
  ["", ["overview", "projects", "sessions", "costs"]],
  ["Analysis", ["context", "tools", "misc"]],
  ["System", ["health"]],
];

const DAY_WINDOWS = [
  [1, "24 h"],
  [7, "7 days"],
  [30, "30 days"],
  [90, "90 days"],
  [0, "All"],
];

// Every request meant to honour the reader's window goes through here: spelled
// out twice, a filter added to one site would leave the other on all of history.
const filterQuery = () =>
  `days=${filters.days}&host=${encodeURIComponent(filters.host)}&project=${encodeURIComponent(filters.project)}`;

// Fetches a route's payload and the header text that goes with it. `force`
// bypasses the payload cache when the reader asked for fresh data.
async function loadPage(page, arg, force) {
  if (page === "session") {
    const url = `/api/session?id=${encodeURIComponent(arg || "")}`;
    const pageData = await cachedJson(url, { force, payloadStatus: 404 });
    // No header can be built from a session that is not there.
    if (pageData.error) {
      return {
        pageData,
        title: "Session not found",
        sub: "No session at this address",
        backLink: `<a class=back href="#/sessions">←</a>`,
      };
    }
    const head = pageData.head;
    return {
      pageData,
      title: head.title || head.project || "Session",
      sub: sessionSubtitle(head, arg),
      backLink: `<a class=back href="#/sessions">←</a>`,
    };
  }
  const route = ROUTES.get(page);
  // An unknown hash gets no request: the raw 404 says less than the message.
  if (!route) {
    return { pageData: null, title: "Not found", sub: "No view at this address", backLink: "" };
  }
  const pageData = await cachedJson(`${route.endpoint}?${filterQuery()}`, { force });
  return { pageData, title: route.label, sub: route.sub, backLink: "" };
}

// A session detail is already bounded, so it gets no select at all. Diagnostics
// bounds its scans by the window, so it keeps the day select; it stays global
// across hosts and projects on purpose, so it drops those two -- narrowing them
// would hide the misconfigured one you came looking for.
function renderSelects(page, filterOptions) {
  if (page === "session") return "";
  const days = DAY_WINDOWS.map(
    ([value, label]) =>
      `<option value=${value} ${filters.days === value ? "selected" : ""}>${label}</option>`,
  ).join("");
  const daysSelect = `<select id=days class="max-md:min-w-0 max-md:max-w-full">${days}</select>`;
  if (page === "health") return daysSelect;
  // An <option> with no value reports its text stripped, so a name with a
  // leading space would not re-select. Hence the explicit value.
  const option = (x, current) =>
    `<option value="${escapeHtml(x)}" ${current === x ? "selected" : ""}>${escapeHtml(x)}</option>`;
  const hosts = filterOptions.hosts.map((x) => option(x, filters.host)).join("");
  const projects = filterOptions.projects.map((x) => option(x, filters.project)).join("");
  // max-w-full caps a select but not what it asks for, and a select is as wide as
  // its widest option -- a project path. min-w-0 lets the three shrink, flex-1
  // hands the leftover to the one holding the paths: four lines down to two.
  return `${daysSelect}
        <select id=host class="max-md:min-w-0 max-md:max-w-full"><option value="">All hosts</option>${hosts}</select>
        <select id=project class="max-md:min-w-0 max-md:flex-1 max-md:max-w-full"><option value="">All projects</option>${projects}</select>`;
}

// The refresh controls are on every view: watching a live session would otherwise
// mean arming Auto 10s elsewhere and remembering it is on.
function renderFilters(page, filterOptions) {
  return `${renderSelects(page, filterOptions)}
        <button id=auto class="${timer ? "on" : ""}">Auto 10s</button>
        <span class=dim style="font-size:12px">updated <span id=since>0</span> ago</span>`;
}

// The session detail is the one route taking an argument, so it stays out of ROUTES.
function renderPageBody(page, pageData) {
  if (page === "session") return pages.session(pageData);
  const route = ROUTES.get(page);
  if (route) return route.render(pageData);
  return `<div class=empty>Unknown page</div>`;
}

const MODAL_VIEWS = new Map([
  ["ev", (m) => detailView(m.d)],
  ["sub", (m) => subagentDetail(m.d)],
  ["hook", (m) => hookDetail(m.d)],
  ["calls", (m) => callsModal(m.label, m.d)],
  ["prompt", (m) => promptDetail(m.d)],
  ["comp", (m) => compactionsModal(m.d.compactions, m.d.context)],
]);

// A stack and not one field per kind: the same kind can appear twice in a chain,
// and what belongs on top is whichever was opened last.
function renderOpenModal() {
  const top = modals.at(-1);
  return top ? `<div class=modal>${MODAL_VIEWS.get(top.k)(top)}</div>` : "";
}

// `source` is an endpoint to fetch, or the payload when the page already holds it.
// `label` is read by the calls modal alone, which titles itself with what was clicked.
async function openModal(kind, source, label) {
  const d = typeof source === "string" ? await fetchJson(source) : source;
  modals.push({ k: kind, d, label });
  return reload();
}

// Main render entry, driven by the URL hash (#/page or #/session/<id>). `force`
// carries through an explicit refresh, past the payload cache to the network.
async function route(force = false) {
  const hash = location.hash.slice(2) || "overview";
  const [page, arg] = hash.split("/");
  qs("#nav").innerHTML = NAV_GROUPS.map(
    ([heading, keys]) =>
      (heading ? `<h6>${heading}</h6>` : "") +
      keys
        .map((key) => {
          const r = ROUTES.get(key);
          return `<a href="#/${key}" class="${key === page ? "on" : ""}">
    <i>${escapeHtml(r.icon)}</i>${escapeHtml(r.label)}</a>`;
        })
        .join(""),
  ).join("");
  const { pageData, title, sub, backLink } = await loadPage(page, arg, force);
  pageState.data = pageData;
  // Fetched once and served from cache thereafter. The options depend on the data
  // that has landed, not on the current window, so no sort, filter or Auto tick
  // refetches them; only the refresh button drops the entry to pick up a new host
  // or project.
  const filterOptions = await cachedJson("/api/filters");
  qs("#main").innerHTML = `
    <div class="head max-md:flex-col max-md:gap-3"><div style="display:flex;gap:13px;align-items:center;min-width:0">${backLink}
      <div class=min-w-0><h1>${escapeHtml(title)}</h1><div class=sub>${sub}</div></div></div>
      <div class="filters max-md:w-full">
        ${renderFilters(page, filterOptions)}
        <button id=refresh title="Refresh">↻</button>
      </div></div>${renderPageBody(page, pageData)}${renderOpenModal()}`;
  // Below md a tab strip scrolls sideways, and rewriting #main put it back at
  // offset 0. Every strip, since a session detail nests one inside another; and
  // scrollLeft rather than scrollIntoView(), which also scrolls the page.
  for (const activeTab of document.querySelectorAll(".tabs button.on")) {
    activeTab.parentElement.scrollLeft = activeTab.offsetLeft - 40;
  }
  clearInterval(tick);
  tick = setInterval(() => {
    const sinceEl = qs("#since");
    if (sinceEl) sinceEl.textContent = Math.round((Date.now() - lastFetch) / 1000) + "s";
  }, 1000);
}

// `payloadStatus` names a failure status whose body is still an answer to render,
// and only /api/session has one: its id comes from the address bar, so an id
// naming nothing is a state of the view rather than a failure.
async function fetchJson(url, payloadStatus = 0) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok && response.status !== payloadStatus) throw new Error(url);
  lastFetch = Date.now();
  return response.json();
}

// The page and filter requests go through here: a sort, page or tab switch keeps
// the URL, so the cached payload serves without a fetch. `force` bypasses and
// refreshes the entry -- the refresh button, Auto 10s and a filter change, where
// the time-based server cache could otherwise return a stale payload.
async function cachedJson(url, { force = false, payloadStatus = 0 } = {}) {
  if (!force && cache.has(url)) return cache.get(url);
  const data = await fetchJson(url, payloadStatus);
  cache.set(url, data);
  return data;
}

// Shows an error inline instead of throwing.
const reload = (force) =>
  route(force).catch(
    (e) => (qs("#main").innerHTML = `<div class="note err">Error: ${escapeHtml(e.message)}</div>`),
  );

// Two delegated listeners handle the whole UI: the body is re-rendered wholesale,
// so per-element handlers would not survive.
document.addEventListener("change", (e) => {
  // The page select alone. Its arrows stay on the click path, routed by data-page.
  const jump = e.target.closest("select[data-pager]");
  if (jump) {
    pager[jump.dataset.pager] = +jump.value;
    reload();
    return;
  }
  if (["days", "host", "project"].includes(e.target.id)) {
    // A select reports a string, and `days` is a number everywhere it is read.
    filters[e.target.id] = e.target.id === "days" ? +e.target.value : e.target.value;
    // Every list, not one: page 3 of the old list means nothing in the new one.
    for (const id of Object.keys(pager)) {
      delete pager[id];
    }
    // A new window is a new cache key, but a revisited one would serve a payload
    // the server has since let expire; force so the reader always sees it fresh.
    reload(true);
  }
});
// A click is routed by its data-* attribute across handleChrome, handleNavigation,
// handleControls and handleRowClick, in that order. Each calls reload() itself and
// returns true once it has taken the click.

function handleChrome(t) {
  if (t.closest("#theme")) {
    // A stylesheet swap, so no reload(): it would redraw identical markup.
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("ccdash-theme", next);
    return true;
  }
  if (t.closest("#menu")) {
    // The drawer is a CSS state too, so no reload() for a transform.
    const root = document.documentElement;
    if (root.dataset.menu === "open") {
      delete root.dataset.menu;
    } else {
      root.dataset.menu = "open";
    }
    syncDrawer();
    return true;
  }
  // The scrim covers everything under an open drawer: a click on it is a click out.
  if (t.closest("#scrim")) {
    closeMenu();
    return true;
  }
  if (t.closest("#refresh")) {
    // The one place that re-reads the filter options: a refresh is the reader
    // asking for everything the cache is holding back, the dropdowns included.
    cache.delete("/api/filters");
    reload(true);
    return true;
  }
  // The backdrop closes the whole stack; Escape and the close button pop one.
  if (t.classList.contains("modal")) {
    modals = [];
    reload();
    return true;
  }
  if (t.id !== "auto") return false;
  if (timer) {
    clearInterval(timer);
    timer = null;
  } else {
    timer = setInterval(() => reload(true), 10000);
  }
  t.classList.toggle("on");
  return true;
}

// A closed drawer is only translated off screen, so its links stay focusable and a
// keyboard reaches the page through an invisible menu. `inert` takes them out, and
// only below md, where the drawer is a drawer at all.
function syncDrawer() {
  const open = document.documentElement.dataset.menu === "open";
  qs("#menu").setAttribute("aria-expanded", open ? "true" : "false");
  qs("aside").toggleAttribute("inert", !open && !matchMedia("(min-width: 768px)").matches);
}

// Below 768px the drawer covers the page, so anything navigating puts it away first.
const closeMenu = () => {
  delete document.documentElement.dataset.menu;
  syncDrawer();
};

// Writing the hash already in the URL fires no hashchange event, so the
// re-render has to be triggered by hand.
function goToHash(target) {
  if (location.hash === target) return reload();
  location.hash = target;
}

function handleNavigation(t, e) {
  const navLink = t.closest("#nav a");
  if (navLink) {
    e.preventDefault();
    closeMenu();
    // Clears what a project drill-down set; host and days were chosen by hand.
    filters.project = "";
    goToHash(navLink.getAttribute("href"));
    return true;
  }
  const go = t.closest("[data-goto]");
  if (!go) return false;
  if (go.dataset.project) filters.project = go.dataset.project;
  // A session link can live inside a modal.
  modals = [];
  goToHash("#/" + go.dataset.goto);
  return true;
}

function handleControls(t) {
  const tb = t.closest("[data-tab]");
  if (tb) {
    tab[tb.dataset.tab] = tb.dataset.v;
    reload();
    return true;
  }
  const pg = t.closest("[data-page]");
  if (pg) {
    if (pg.disabled) return true;
    pager[pg.dataset.pager] = +pg.dataset.page;
    reload();
    return true;
  }
  // The close button always belongs to the modal on top, so it names no kind.
  if (t.closest("[data-close]")) {
    modals.pop();
    reload();
    return true;
  }
  const th = t.closest("th");
  if (!th?.dataset.k) return false;
  const id = th.closest("table").dataset.t;
  const k = th.dataset.k;
  // Re-clicking the sorted column flips it; a new column starts descending.
  sort[id] = sort[id] && sort[id].k === k ? { k, d: -sort[id].d } : { k, d: -1 };
  // Re-ordering moves every row off the page the reader was on.
  delete pager[id];
  reload();
  return true;
}

// [table id suffix, kind of modal, endpoint for the row]. A suffix and not the
// whole id: the scope prefixes it, "gsubc" globally against "ssubc" in a session.
const ROW_MODALS = [
  ["acalls", "ev", (id) => `/api/event?id=${encodeURIComponent(id)}`],
  ["subc", "sub", (id) => `/api/subagent?id=${encodeURIComponent(id)}`],
  ["bashd", "ev", (id) => `/api/event?id=${encodeURIComponent(id)}`],
  ["errd", "ev", (id) => `/api/event?id=${encodeURIComponent(id)}`],
  ["apierr", "ev", (id) => `/api/event?id=${encodeURIComponent(id)}`],
  ["hk", "hook", (id) => `/api/hook?name=${encodeURIComponent(id)}`],
  ["prompts", "prompt", (id) => `/api/prompt?id=${encodeURIComponent(id)}`],
];

async function handleRowClick(t) {
  // Tested before [data-ev]: the link sits inside an event detail it stacks on.
  const pl = t.closest("[data-prompt]");
  if (pl) {
    return openModal("prompt", `/api/prompt?id=${encodeURIComponent(pl.dataset.prompt)}`);
  }
  const iv = t.closest("[data-ev]");
  if (iv) {
    return openModal("ev", `/api/event?id=${encodeURIComponent(iv.dataset.ev)}`);
  }
  if (t.closest("[data-comp]")) return openModal("comp", pageState.data);
  const tr = t.closest("tr[data-id]");
  if (!tr) return;
  const table = tr.closest("table").dataset.t;
  const modal = ROW_MODALS.find(([suffix]) => table.endsWith(suffix));
  if (modal) {
    const [, kind, url] = modal;
    return openModal(kind, url(tr.dataset.id));
  }
  // The Files tab exists on a session detail alone, so that session is the scope.
  if (table.endsWith("files")) {
    const [, sessionId] = location.hash.slice(2).split("/");
    return openModal(
      "calls",
      `/api/calls?file=${encodeURIComponent(tr.dataset.id)}&session=${encodeURIComponent(sessionId)}`,
      tr.dataset.id.split("/").pop(),
    );
  }
  // The drill-down follows the scope it was opened from: one turn inside a prompt
  // modal, one session in a session detail, the global filter otherwise.
  if (table.endsWith("tools")) {
    const [page, arg] = location.hash.slice(2).split("/");
    const prompt = modals.findLast((m) => m.k === "prompt");
    let scope = "";
    if (prompt) {
      scope = `&prompt=${encodeURIComponent(prompt.d.id)}`;
    } else if (page === "session") {
      scope = `&session=${encodeURIComponent(arg)}`;
    }
    return openModal(
      "calls",
      `/api/calls?label=${encodeURIComponent(tr.dataset.id)}&${filterQuery()}${scope}`,
      tr.dataset.id,
    );
  }
}

document.addEventListener("click", async (e) => {
  const t = e.target;
  if (handleChrome(t) || handleNavigation(t, e) || handleControls(t)) return;
  await handleRowClick(t);
});
addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !modals.length) return;
  // One level per press: an event opened from the calls list returns to it.
  modals.pop();
  reload();
});
// Above 768px the toggle is hidden, so a leftover open state could not be cleared.
matchMedia("(min-width: 768px)").addEventListener("change", (e) => {
  if (e.matches) return closeMenu();
  syncDrawer();
});
addEventListener("hashchange", () => {
  // What a nav click does not cover: the back button, a hash typed by hand.
  closeMenu();
  modals = [];
  tab.sess = null;
  reload();
});
syncDrawer();
reload();
