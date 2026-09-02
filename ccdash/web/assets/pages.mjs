import { tab } from "./state.mjs";
import {
  escapeHtml,
  estTokens,
  formatBytes,
  formatDate,
  formatDateTime,
  formatDuration,
  formatMoney,
  formatNumber,
  formatTime,
  modelColor,
  TOKEN_TYPES,
} from "./format.mjs";
import {
  stackedAreaChart,
  rhythmGrid,
  horizontalBars,
  weightBar,
  contextSparkline,
} from "./charts.mjs";
import { paginate, renderTabs, statCard } from "./components.mjs";
import {
  eventNameTable,
  fileTable,
  hookTable,
  idleTable,
  ingestTable,
  metricNameTable,
  noteTable,
} from "./tables.mjs";
import { analysisTabs, SESSION } from "./analysis.mjs";

// One renderer per top-level route, each taking the endpoint payload and returning
// the page markup. The only consumer of analysisTabs.
const pages = {};

// What fits next to the rhythm grid without stretching the page.
const PROJECTS_PER_PAGE = 5;

// Nothing without a `prev`: the "All" window has no before, and a history shorter
// than the window would read as a plunge to -100%.
const deltaTag = (prev, current, key) => {
  if (!prev?.[key]) return "";
  const pct = (100 * (current[key] - prev[key])) / prev[key];
  // Rounding to nothing keeps its slot: an arrow would claim a direction, and a
  // missing tag would break the alignment of the cards.
  const rounded = Math.abs(pct).toFixed(0);
  if (rounded === "0") return ` &middot; <span class=dl>= 0%</span>`;
  return ` &middot; <span class="dl ${pct > 0 ? "up" : "down"}">${pct > 0 ? "▲" : "▼"} ${rounded}%</span>`;
};

// Counting the cache *write* as the miss is what makes the figure move: read over
// read+input sits above 98% by construction and separates nothing. One definition
// for the Overview and a session alike; two would be two meanings of one word.
const cacheMissPct = (t) =>
  (100 * (t.cache_creation || 0)) /
  Math.max(1, (t.cache_read || 0) + (t.cache_creation || 0) + (t.input || 0));

// Reconciles the raw counts a session prints with the weight its bar prints.
// A decimal under one point: the fresh input share would otherwise read "0%".
const weightShare = (weighted, type) => {
  const total = Object.values(weighted).reduce((sum, v) => sum + v, 0);
  if (!total) return "";
  const pct = (100 * (weighted[type] || 0)) / total;
  return `${pct < 1 ? pct.toFixed(1) : pct.toFixed(0)}%`;
};

// `data` is /api/overview. No trend and no weighted breakdown: spending is a
// Costs figure.
pages.overview = (data) => {
  const { kpi, prev, tokens, rhythm, projects, model_calls, skills, mcp, delegation_types } = data;
  const delta = (key) => deltaTag(prev, kpi, key);
  // `model_calls` is the one breakdown that arrives as a map rather than a list.
  const modelEntries = Object.entries(model_calls)
    .map(([label, value]) => ({ label, value }))
    .sort((a, b) => b.value - a.value);
  const netLines = kpi.loc_add - kpi.loc_del;
  return `
  <div class=cards>
    ${statCard("Sessions", formatNumber(kpi.sessions), `${kpi.prompts} prompts`, "\u25F1", "", delta("sessions"))}
    ${statCard(
      "Tool calls",
      formatNumber(kpi.tool_calls),
      `${formatNumber(kpi.commits)} commits, ${netLines >= 0 ? "+" : ""}${formatNumber(netLines)} net lines`,
      "\u2692",
      "",
      delta("tool_calls"),
    )}
    ${statCard("Tokens", formatNumber(kpi.tokens), `cache miss ${cacheMissPct(tokens).toFixed(1)}%`, "\u223F", "", delta("tokens"))}
    ${statCard("Est. cost", formatMoney(kpi.cost), `${formatDuration(kpi.active_seconds)} active`, "\u25CE", "", delta("cost"))}
  </div>
  <div class="two thirds">
    <div class=box><h2>Rhythm</h2>
      <p class=cap>Telemetry points per weekday and hour</p>${rhythmGrid(rhythm)}</div>
    <div class=box><h2>Projects</h2>
      <p class=cap>Most recently active first &mdash; opens the sessions of that project</p>
      ${projectRows(projects)}</div>
  </div>
  <div class=three>
    <div class=box><h2>Models</h2>
      <p class=cap>API calls answered per family</p>
      ${
        modelEntries.length
          ? horizontalBars(modelEntries, { fmt: formatNumber })
          : `<div class=empty>No model recorded in this period</div>`
      }</div>
    <div class=box><h2>Skills &amp; MCP</h2>
      <p class=cap>Activations and calls</p>
      ${
        skills.length || mcp.length
          ? namedBars("Skills", skills) + namedBars("MCP servers", mcp)
          : MISSING_DETAILS("No skill or MCP server used in this period.")
      }</div>
    <div class=box><h2>Sub-agents</h2>
      <p class=cap>Delegations per agent type</p>
      ${
        delegation_types.length
          ? horizontalBars(
              delegation_types.map((a) => ({ label: a.agent_type, value: a.calls })),
              { fmt: formatNumber },
            )
          : MISSING_DETAILS("No sub-agent called in this period.")
      }</div>
  </div>`;
};

// Each list keeps its heading when empty, or the box reads as if the other list
// were the whole story.
const namedBars = (heading, items) =>
  SUB_HEADING(heading) +
  (items.length
    ? horizontalBars(
        items.map((item) => ({ label: item.name, value: item.uses })),
        { fmt: formatNumber },
      )
    : `<p class=cap>None</p>`);

const SUB_HEADING = (text) => `<h3 style="font-size:12.5px;margin:14px 0 6px">${text}</h3>`;

// Without the flag these names are stripped, so an empty box means "not
// collected" rather than "never used".
const MISSING_DETAILS = (what) =>
  `<div class=empty>${what} Names are only recorded with
  <code>OTEL_LOG_TOOL_DETAILS=1</code>.</div>`;

// Rows and not cards: the shape of the sessions list, which is where a click
// leads. The Projects page shows everything and needs no pager.
function projectRows(projects, paged = true) {
  if (!projects.length) {
    return `<div class=empty>No project. Without a <code>project</code> attribute, everything is
    grouped under "(undefined)".</div>`;
  }
  // The payload is sorted by cost for the Costs bar chart; this list is a way in,
  // so it follows activity instead.
  const sorted = [...projects].sort((a, b) => (b.last || 0) - (a.last || 0));
  const { visible, control } = paged
    ? paginate("projects", sorted, PROJECTS_PER_PAGE)
    : { visible: sorted, control: "" };
  const rows = visible.map(
    (project) => `
  <div class="row max-md:flex-col max-md:items-start max-md:gap-2" data-goto="sessions" data-project="${escapeHtml(project.project)}">
    <div class=id><div class=t>${escapeHtml(project.project)}
      ${project.models.map((m) => `<span class="tag ${escapeHtml(m)}">${escapeHtml(m)}</span>`).join("")}</div>
      <div class=m>${project.last ? escapeHtml(formatDateTime(project.last)) : "never active"}</div></div>
    <div class="g max-md:w-full max-md:grid-flow-row max-md:grid-cols-2"><div><b>${escapeHtml(formatNumber(project.sessions))}</b><i>sessions</i></div>
      <div><b>${escapeHtml(formatNumber(project.tools))}</b><i>tools</i></div>
      <div><b>${escapeHtml(formatNumber(project.tokens))}</b><i>tokens</i></div>
      <div><b>${escapeHtml(formatMoney(project.cost))}</b><i>cost</i></div></div></div>`,
  );
  return `<div class=rows>${rows.join("")}</div>${control}`;
}

pages.projects = (projects) => `<div class=box>${projectRows(projects, false)}</div>`;

// From /api/sessions: `median` and `sessions` both cover the whole period, so
// `median.sessions` is the list length and the medians read as the period's.
pages.sessions = ({ sessions, median }) => {
  if (!sessions.length) {
    return `<div class="box rows"><div class=empty>No sessions in this period</div></div>`;
  }
  // `idle` is what the medians ignored, said here rather than left as a list
  // quietly shorter than the period. Diagnostics lists them.
  const scope =
    `across ${escapeHtml(formatNumber(median.sessions))} sessions` +
    (median.idle ? ` · ${escapeHtml(formatNumber(median.idle))} idle left out` : "");
  const cards = `
  <div class=cards>
    ${statCard("Median active time", formatDuration(median.active_seconds), scope, "◴")}
    ${statCard("Median prompts", formatNumber(median.prompts), "", "✎")}
    ${statCard("Median tokens", formatNumber(median.tokens), "", "∿")}
    ${statCard("Median producing", `${median.output_weight_pct.toFixed(0)}%`, "", "◨")}
    ${statCard("Median cost", formatMoney(median.cost), "", "◎")}
  </div>`;
  const { visible, control } = paginate("sessions", sessions);
  const rows = visible.map(
    (session) => `
  <div class="row max-md:flex-col max-md:items-start max-md:gap-2" data-goto="session/${escapeHtml(session.session_id)}">
    <div class=id><div class=t>${escapeHtml(session.title || session.project || "(undefined)")}
      ${session.title ? `<span class=tag>${escapeHtml(session.project || "-")}</span>` : ""}
      ${session.title_src === "rename" ? `<span class="tag Skill">renamed</span>` : ""}
      ${session.models.map((m) => `<span class="tag ${escapeHtml(m)}">${escapeHtml(m)}</span>`).join("")}</div>
      <div class=m><span>${escapeHtml(formatDateTime(session.ended_at))}</span>
      ${session.compactions ? `<span class=amber>\u21B4 ${escapeHtml(formatNumber(session.compactions))} compactions</span>` : ""}
      ${session.host ? `<span>${escapeHtml(session.host)}</span>` : ""}</div></div>
    <div class="g max-md:w-full max-md:grid-flow-row max-md:grid-cols-3"><div><b>${escapeHtml(formatDuration(session.ended_at - session.started_at))}</b><i>duration</i></div>
      <div><b>${escapeHtml(formatNumber(session.prompts))}</b><i>prompts</i></div>
      <div><b>${escapeHtml(formatNumber(session.tools))}</b><i>tools</i></div>
      <div><b>${escapeHtml(formatNumber(session.tokens))}</b><i>tokens</i></div>
      <div><b>${session.output_weight_pct.toFixed(0)}%</b><i>producing</i></div>
      <div><b>${escapeHtml(formatMoney(session.cost))}</b><i>cost</i></div></div></div>`,
  );
  return `${cards}<div class="box rows">${rows.join("")}${control}</div>`;
};

// What a `truncated` key is called on screen, so the warning names the tab to look
// in. A Map: the key is a payload value, and a literal answers `constructor`.
const TRUNCATED_LABELS = new Map([
  ["events", "Timeline"],
  ["bash", "Bash"],
  ["subagents", "Sub-agents"],
  ["errors", "Failures"],
  ["api_errors", "API errors"],
  ["files", "Files"],
  ["prompts", "Prompts"],
]);

// A fire costs a few tens of milliseconds, so this marks the handful that stand
// out rather than the population. Amber and not `ko`: slow is not failed.
const HOOK_SLOW_MS = 500;

// Timeline rows whose detail follows from the kind of record alone. A Map: the key
// is a payload value, and `constructor` would resolve against the prototype.
const EVENT_DETAILS = new Map([
  [
    "compaction",
    (event) =>
      `${escapeHtml(event.trigger_kind)} &middot; ${escapeHtml(formatNumber(event.pre_tokens))} \u2192 ${escapeHtml(formatNumber(event.post_tokens))} tokens`,
  ],
  [
    "tool_decision",
    (event) => `${escapeHtml(event.decision)} (${escapeHtml(event.dec_source || "")})`,
  ],
  ["skill_activated", (event) => escapeHtml(event.skill_name || "")],
  [
    "user_prompt",
    (event) =>
      `<span style=color:var(--tx)>${escapeHtml(event.prompt_text || "(redacted)")}</span>`,
  ],
  [
    "assistant_response",
    (event) => {
      // Claude Code sends the word itself when the flag is off: not an unanswered row.
      if (event.response == null || event.response === "<REDACTED>") return "(redacted)";
      // `response_length` is what Claude Code sent, so a clipped row says so
      // rather than ending mid-word.
      const cut = event.response_length > event.response.length ? "&hellip;" : "";
      return `<span style=color:var(--tx)>${escapeHtml(event.response)}${cut}</span>`;
    },
  ],
  [
    "api_request",
    (event) =>
      `${escapeHtml(event.model || "")}${
        event.duration_ms ? " &middot; " + escapeHtml(formatDuration(event.duration_ms / 1000)) : ""
      }`,
  ],
  [
    "permission_mode_changed",
    (event) =>
      `${escapeHtml(event.from_mode || "?")} → ${escapeHtml(event.to_mode || "?")}` +
      // `trigger` is not always exported, and empty brackets read as a failure.
      (event.trigger_kind ? ` (${escapeHtml(event.trigger_kind)})` : ""),
  ],
  [
    "mcp_server_connection",
    (event) =>
      (event.mcp_status === "failed"
        ? `<span class=ko>${escapeHtml(event.mcp_status)}</span>`
        : escapeHtml(event.mcp_status || "?")) +
      ` &middot; ${escapeHtml(event.mcp_name || "?")}` +
      (event.mcp_transport ? " &middot; " + escapeHtml(event.mcp_transport) : ""),
  ],
  [
    "hook_execution_complete",
    (event) => {
      const name = escapeHtml(event.hook_name || "?");
      if (event.hook_ms == null) return name;
      const took = `${escapeHtml(Math.round(event.hook_ms))} ms`;
      return `${name} &middot; ${
        event.hook_ms > HOOK_SLOW_MS ? `<span class=amber>${took}</span>` : took
      }`;
    },
  ],
  [
    "api_error",
    (event) =>
      `<span class=ko>${escapeHtml(String(event.status_code ?? "?"))}</span>` +
      (event.error_msg ? " &middot; " + escapeHtml(event.error_msg) : ""),
  ],
  [
    "api_retries_exhausted",
    (event) =>
      `<span class=ko>${escapeHtml(event.attempts || "?")} attempts</span>` +
      (event.retry_ms ? " &middot; " + escapeHtml(formatDuration(event.retry_ms / 1000)) : ""),
  ],
  ["internal_error", (event) => `<span class=ko>${escapeHtml(event.error_name || "?")}</span>`],
  // `success` is left alone: an at-mention carries `false` on nearly every record,
  // undocumented, and reading it as a failure painted the whole population red.
  ["at_mention", (event) => escapeHtml(event.mention_type || "")],
]);

// One line for a timeline event: the kind of record decides it when it can,
// otherwise what the record carries does, most specific first.
const describeEvent = (event) => {
  const byKind = EVENT_DETAILS.get(event.name);
  if (byKind) return byKind(event);
  // `hook_registered` alone reaches this: EVENT_DETAILS takes the fire itself.
  if (event.hook_name) return escapeHtml(event.hook_name);
  if (event.agent_type)
    return (
      `<span class="tag Agent">${escapeHtml(event.agent_type)}</span>` +
      (event.agent_desc ? " &middot; " + escapeHtml(event.agent_desc) : "")
    );
  if (event.success === false)
    return `<span class=ko>\u2717 ${escapeHtml(event.error_type || "failure")}</span>`;
  // `mcp:server/tool`, `skill:name`: the label refines the tool name rather than
  // replacing it, and refines nothing without one.
  if (event.tool_name && event.label !== event.tool_name)
    return (
      escapeHtml(event.label) +
      (event.result_bytes == null
        ? ""
        : ` &middot; ${escapeHtml(formatBytes(event.result_bytes))} &middot; ${estTokens(
            event.result_bytes,
          )} tok`)
    );
  if (event.result_bytes != null)
    return (
      `${escapeHtml(formatBytes(event.result_bytes))} &middot; ${estTokens(event.result_bytes)} tok` +
      // `agent_desc` is the tool's own `description`; the whole command is in
      // the event inspector, unclipped.
      (event.bash_cmd
        ? " &middot; " + escapeHtml(event.agent_desc || event.bash_cmd.slice(0, 80))
        : "") +
      // The name alone: the row already carries the size, and the directory is
      // the same for every row of a session spent in one project.
      (event.file_path ? " &middot; " + escapeHtml(event.file_path.split("/").pop()) : "")
    );
  return escapeHtml(event.name);
};

// The kind of row, against describeEvent's account of what is specific to it.
const nameEvent = (event) => {
  // A sub-agent's report comes back through the prompt channel, and is not the
  // kind of row a typed prompt opens.
  if (event.name === "user_prompt" && event.prompt_text?.startsWith("<task-notification>")) {
    return "task-notification";
  }
  return escapeHtml(event.tool_name || event.name);
};

// `context` is the curve of prompt sizes and `compactions` the drops in it. A
// session predating the curve carries neither.
function contextBox(context = [], compactions = []) {
  if (!context.length) {
    return `<div class=box><h2>Context</h2><div class=empty>No API requests recorded. The context
    curve is read off them, which needs <code>OTEL_LOGS_EXPORTER=otlp</code>.</div></div>`;
  }
  const current = context.at(-1).value;
  // Starting at the compaction before last holds the live stretch and one drop.
  // Drawn whole, a dozen compactions make a sawtooth the live part is a sliver of.
  const curveStart = compactions.at(-2)?.ts;
  const curve = curveStart ? context.filter((p) => p.ts >= curveStart) : context;
  const dropped = Math.max(compactions.length - 2, 0);
  const plural = dropped > 1 ? "s are" : " is";
  const cut = dropped
    ? ` The curve starts at the previous compaction;
      ${escapeHtml(formatNumber(dropped))} earlier compaction${plural} off it.`
    : "";
  const lastComp = compactions.at(-1);
  const since = context.filter((p) => !lastComp || p.ts > lastComp.ts);
  const low = since.length ? Math.min(...since.map((p) => p.value)) : current;
  const sinceRow = panelRow(
    lastComp ? "Since last compaction" : "Over the session",
    `${escapeHtml(formatNumber(low))} <span class=dim>&rarr;</span> ${escapeHtml(formatNumber(current))}`,
  );
  const compCount = compactions.length
    ? `<span data-comp style="cursor:pointer;color:var(--amber)">${escapeHtml(formatNumber(compactions.length))}
      <span class=dim style=font-size:11px>&#9656;</span></span>`
    : "0";
  return `<div class=box><div class=boxhead><h2>Context</h2>
    <span class=tot>${escapeHtml(formatNumber(current))} <span class=dim>tokens</span></span></div>
    <p class=cap>How big the conversation is now, not what it has spent: the
    prompt size of the last main-thread request &mdash; fresh input, cache reads
    and cache writes together.${cut}</p>
    ${contextSparkline(curve, compactions)}
    <div style="display:flex;justify-content:space-between;padding-top:3px">
      <span class=dim style=font-size:11px>${escapeHtml(formatTime(curve[0].ts))}</span>
      <span class=dim style=font-size:11px>${escapeHtml(formatTime(curve.at(-1).ts))}</span></div>
    ${sinceRow}${panelRow("Compactions", compCount)}</div>`;
}

// flex-wrap: "Since last compaction" against 180px of tags is 310px in a 297px
// box, and a nowrap row has nowhere to put the overflow.
const panelRow = (label, value) =>
  `<div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:4px 12px;padding:7px 0;
    border-bottom:1px solid var(--grid)"><span class=dim>${label}</span>
    <span class=num>${value}</span></div>`;

// A segment with nothing behind it is dropped whole, or a separator would stand
// for a value that failed to print. Terminals and versions are lists: a session
// that changed terminal or saw the CLI update shows every value it ran under.
export const sessionSubtitle = (head, sessionId) => {
  const terminals = (head.terminals || []).map((t) => escapeHtml(t)).join(", ");
  const versions = (head.versions || []).map((v) => escapeHtml(v)).join(", ");
  return [
    head.project ? escapeHtml(head.project) : "",
    escapeHtml(sessionId.slice(0, 8)),
    escapeHtml(formatDateTime(head.started_at)),
    head.title_src === "rename" ? "renamed" : "",
    terminals,
    // Named: a bare version number would read as the dashboard's own.
    versions ? "Claude Code " + versions : "",
  ]
    .filter(Boolean)
    .join(" &middot; ");
};

// Rendering every row of a session this size would freeze the tab, and a list
// ending without a word reads as the whole of it.
const truncationNote = (truncated) =>
  truncated.length
    ? `<div class=note>This session is too large to show whole. Only its most recent rows
    are on this page, under: ${truncated.map((k) => escapeHtml(TRUNCATED_LABELS.get(k) || k)).join(", ")}.</div>`
    : "";

// `head` carries no byte total, so the results card sums the per-tool aggregate.
const sessionCards = ({ head, tools, prompts }) => {
  const resultBytes = tools.reduce((sum, tool) => sum + tool.total_bytes, 0);
  return `<div class=cards>
    ${statCard("Duration", formatDuration(head.ended_at - head.started_at), "active " + formatDuration(head.active_seconds), "\u25F4")}
    ${statCard("Prompts", formatNumber(prompts.length), "", "\u25B8")}
    ${statCard(
      "Tool calls",
      formatNumber(tools.reduce((sum, tool) => sum + tool.calls, 0)),
      formatNumber(tools.reduce((sum, tool) => sum + tool.failures, 0)) + " failures",
      "\u2692",
    )}
    ${statCard("Tool results", formatBytes(resultBytes), "", "\u2263", "", `${estTokens(resultBytes)} tokens read back`)}
    ${statCard("Cost", formatMoney(head.cost), "", "\u25CE")}
  </div>`;
};

const sessionTabStrip = (
  { events, tools, files, bash, prompts, subagents, errors, api_errors, decisions },
  view,
) =>
  renderTabs("sess", view, [
    ["flow", "Timeline", events.length],
    ["tools", "Tools", tools.length],
    ["files", "Files", files.length],
    ["bash", "Bash", bash.length],
    ["prompts", "Prompts", prompts.length],
    ["agents", "Sub-agents", subagents.length],
    ["misc", "Other", errors.length + api_errors.length + decisions.length],
  ]);

// One row per event, newest first, carrying the time alone: a session can span
// several days, so a header is inserted whenever the day changes.
const sessionTimeline = (events) => {
  // Twice the usual page: the rows are one line each and a session runs to the
  // 10 000 the API caps at. The accumulator resets per page, so a day repeats
  // across a page break rather than going unnamed.
  const { visible, control } = paginate("flow", events, 100);
  let day = "";
  const rows = visible
    .map((event) => {
      const eventDay = escapeHtml(formatDate(event.ts));
      const header = eventDay === day ? "" : `<div class=day>${eventDay}</div>`;
      day = eventDay;
      // An event with nothing specific to say falls back to its own kind, which
      // the left column already gives.
      const name = nameEvent(event);
      const detail = describeEvent(event);
      return `${header}<div class="e max-md:flex-wrap ${
        event.name === "compaction" ? "c" : ""
      }" data-ev="${escapeHtml(event.id)}" style=cursor:pointer>
    <time>${escapeHtml(formatTime(event.ts))}</time><span class=n>${name}</span>
    <span class=d>${detail === name ? "" : detail}</span></div>`;
    })
    .join("");
  const empty = "<div class=empty>No events</div>";
  return `<div class=tl>${rows || empty}</div>${control}`;
};

// "flow" and "files" are the two tabs with no global counterpart.
const sessionTabBody = (data, view) => {
  if (view === "flow") return sessionTimeline(data.events);
  if (view === "files") return fileTable("sfiles", data.files);
  return analysisTabs.get(view)(data, SESSION);
};

// What the session ran under, one row per attribute.
const sessionPanel = (head) =>
  `<div class="box" style=margin-top:0><h2>Session</h2><p class=cap>&nbsp;</p>
        ${panelRow("Project", escapeHtml(head.project || "-"))}
        ${panelRow("Host", escapeHtml(head.host || "-"))}
        ${panelRow(
          "Models",
          (head.models || [])
            .map((m) => `<span class="tag ${escapeHtml(m)}">${escapeHtml(m)}</span>`)
            .join("") || "-",
        )}
        ${panelRow(
          "Output style",
          (head.output_styles || [])
            .map((s) => `<span class=tag>${escapeHtml(s)}</span>`)
            .join("") || "-",
        )}
        ${panelRow(
          "Effort",
          (head.efforts || []).map((e) => `<span class=tag>${escapeHtml(e)}</span>`).join("") ||
            "-",
        )}
        ${panelRow(
          "Net lines",
          (head.lines_added - head.lines_removed >= 0 ? "+" : "") +
            escapeHtml(formatNumber(head.lines_added - head.lines_removed)),
        )}</div>`;

const tokensBox = (head) => {
  const totalTokens =
    head.input_tokens + head.cache_read_tokens + head.cache_creation_tokens + head.output_tokens;
  return `<div class=box><div class=boxhead><h2>Cumulative tokens</h2>
        <span class=tot>${escapeHtml(formatNumber(totalTokens))}</span></div>
        <p class=cap>What the whole session has spent, summed over its requests.</p>
        ${[...TOKEN_TYPES]
          .map(
            ([type, { label, cls }]) =>
              `<div style="display:flex;justify-content:space-between;padding:7px 0;
          border-bottom:1px solid var(--grid)"><span class=dim>
          <b style="display:inline-block;width:9px;height:9px;border-radius:3px;
          background:var(--tok-${escapeHtml(cls.slice(2))});margin-right:8px"></b>${label}</span>
          <span class=num>${escapeHtml(formatNumber(head[type + "_tokens"]))}
          <span class=dim>${weightShare(head.weighted, type)}</span></span></div>`,
          )
          .join("")}
        <div style=margin-top:12px>${weightBar(head.weighted)}</div>
        <p class=cap style=margin-top:8px>The count is the volume, the percentage
        and the bar are the weight. Cache miss ${cacheMissPct({ input: head.input_tokens, cache_read: head.cache_read_tokens, cache_creation: head.cache_creation_tokens }).toFixed(1)}%.</p></div>`;
};

// `sources` is one row per query_source; a session with no cost gets a word
// rather than an empty chart.
const originBox = (sources) => {
  const originTotal = sources.reduce((sum, row) => sum + row.cost, 0);
  return `<div class=box><div class=boxhead><h2>Request origin</h2>
        <span class=tot>${escapeHtml(formatMoney(originTotal))}</span></div>
        <p class=cap>Where the spend went. Sub-agents and the calls Claude Code
        makes on its own do not run on the main loop.</p>
        ${
          originTotal
            ? horizontalBars(
                sources.map((row) => ({ label: originName(row.source), value: row.cost })),
              )
            : `<div class=empty>No cost recorded for this session.</div>`
        }</div>`;
};

pages.session = (data) => {
  // The 404 payload, for an id the address bar can produce. Saying so is the whole
  // view: the panels run on aggregates that come back NULL and print a session
  // that did nothing.
  if (data.error) {
    return `<div class="box rows"><div class=empty>No session with this id.
      It may never have been recorded, or the address may be wrong.</div></div>`;
  }
  const view = tab.sess || "flow";
  return `${truncationNote(data.truncated)}${sessionCards(data)}
  <div class=two>
    <div class="box fill">${sessionTabStrip(data, view)}
      <div class=tabbody>${sessionTabBody(data, view)}</div></div>
    <div>
      ${sessionPanel(data.head)}
      ${contextBox(data.context, data.compactions)}
      ${tokensBox(data.head)}
      ${originBox(data.sources)}
    </div></div>`;
};
// A Map, not an object literal: a source named "constructor" would hit
// Object.prototype and land in the DOM as a function.
const ORIGIN_NAMES = new Map([
  ["main", "Main thread"],
  ["repl_main_thread", "Main thread"],
  ["sdk", "Main thread (SDK)"],
  // The metrics know three origins where the events name twelve, so these reach
  // the UI through the `sources` payload alone.
  ["subagent", "Sub-agents"],
  ["auxiliary", "Auxiliary"],
  ["prompt_suggestion", "Autocomplete"],
  ["away_summary", "Background summary"],
  ["compact", "Compaction"],
  ["generate_session_title", "Session title"],
]);
const originName = (s) =>
  ORIGIN_NAMES.get(s) || (s.startsWith("agent:") ? "Sub-agent: " + s.split(":").pop() : s);
pages.costs = (data) => {
  const { tokens, weights, weighted, per_model, series, families, by_project } = data;
  const totalWeight = Object.values(weighted).reduce((sum, v) => sum + v, 0) || 1;
  const totalCost = Object.values(per_model).reduce((sum, m) => sum + (m.cost || 0), 0);
  // Tokens saved by cache reads, expressed as equivalent input tokens.
  const cacheSavedTokens = (tokens.cache_read || 0) * (1 - weights.cache_read);
  const origins = data.origins || [];
  // `is_main_thread` flags them; the names behind it are the API's business.
  const nonMain = origins.filter((o) => !o.is_main_thread).reduce((sum, o) => sum + o.cost, 0);
  const originTotal = origins.reduce((sum, o) => sum + o.cost, 0);
  // Omitted with no cost at all: the percentage would divide by zero.
  const outsideMain = originTotal
    ? `${escapeHtml(formatMoney(nonMain))} (${Math.round((100 * nonMain) / originTotal)}%) outside the main thread`
    : "&nbsp;";
  return `
  <div class=box style=margin-top:0><h2>Cost over time</h2><p class=cap>Stacked by model</p>
    ${stackedAreaChart(series, families)}
    <div class=leg>${families
      .map((f) => `<span><b style="background:${modelColor(f)}"></b>${escapeHtml(f)}</span>`)
      .join("")}</div></div>
  <div class=box><h2>Real weight</h2>
    <p class=cap>What tokens really cost once weighted</p>
    ${weightBar(weighted)}<div class=leg>${[...TOKEN_TYPES]
      .map(
        ([type, { label, cls }]) => `<span><b class=${cls}></b>${label} &mdash;
      <b style="font-weight:600;color:var(--tx)">${((100 * (weighted[type] || 0)) / totalWeight).toFixed(0)}%</b>
      </span>`,
      )
      .join("")}</div></div>
  <div class=cards>
    ${statCard("Total cost", formatMoney(totalCost), "in this period", "\u25CE")}
    ${statCard("Cache savings", formatNumber(cacheSavedTokens) + " eq.", "input tokens saved", "\u26A1")}
    ${statCard("Cache write", formatNumber(tokens.cache_creation), "new context", "\u2191")}
    ${statCard("Output", formatNumber(tokens.output), "tokens", "\u2193")}
  </div>
  <div class=two>
    <div class=box style=margin-top:0><h2>Cost by project</h2><p class=cap>&nbsp;</p>
      ${horizontalBars(by_project.map((p) => ({ label: p.project, value: p.cost })))}</div>
    <div class=box style=margin-top:0><h2>By request origin</h2>
      <p class=cap>${outsideMain}</p>
      ${
        origins.length
          ? horizontalBars(origins.map((o) => ({ label: originName(o.src), value: o.cost })))
          : `<div class=empty>No API request recorded</div>`
      }</div>
  </div>`;
};

// OTLP aggregationTemporality. A Map keeps the keys numeric: "1" must not match.
const TEMPORALITY_NAMES = new Map([
  [1, "delta"],
  [2, "cumulative"],
]);

pages.health = (data) => {
  const {
    metric_points,
    event_names,
    temporality,
    db_size,
    delegation_calls,
    prompts_total,
    prompts_text,
    masked_mcp,
    slash_seen,
    renames,
    unknown,
    ingest,
    metric_names,
    notes,
    hooks,
    idle,
    db,
  } = data;
  // Cumulative totals rather than deltas make every sum wrong.
  const hasCumulative = temporality.includes(2);
  // A hook or a client-side handler can consume a slash command before it is sent.
  const slashNote = slash_seen
    ? "Slash commands do arrive in the events."
    : "No slash command received: they are probably intercepted before export.";
  const eventTotal = event_names.reduce((sum, e) => sum + e.points, 0);
  return `
  <div class=cards>
    ${statCard("Metric points", formatNumber(metric_points), "", "\u25A6")}
    ${statCard("Events", formatNumber(eventTotal), "", "\u25A4")}
    ${statCard(
      "Temporality",
      temporality.map((t) => TEMPORALITY_NAMES.get(t) || t).join(", ") || "unknown",
      hasCumulative ? "wrong sums" : "correct",
      "\u2699",
      hasCumulative ? "hl" : "",
    )}
    ${statCard("Database size", formatBytes(db_size), "", "\u25EC")}
    ${statCard("Delegations", formatNumber(delegation_calls), "Task calls", "\u26AC")}
    ${statCard("Prompts", formatNumber(prompts_total), formatNumber(prompts_text) + " with text", "\u25B8")}
  </div>
  ${
    hasCumulative
      ? `<div class="note err">Cumulative temporality:
    the dashboard sums increments, so totals are wrong. Add
    <code>OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta</code>.</div>`
      : ""
  }
  ${
    !event_names.length && metric_points
      ? `<div class=note>No events received:
    <code>OTEL_LOGS_EXPORTER=otlp</code> is missing. Without it, no tool usage.</div>`
      : ""
  }
  ${
    masked_mcp
      ? `<div class=note>${escapeHtml(formatNumber(masked_mcp))} MCP calls without a server name.
    Add <code>OTEL_LOG_TOOL_DETAILS=1</code> to see your servers.</div>`
      : ""
  }
  ${
    prompts_total && !prompts_text
      ? `<div class=note>Prompt content redacted
    (${escapeHtml(formatNumber(prompts_total))} prompts received, 0 with text). Add
    <code>OTEL_LOG_USER_PROMPTS=1</code> to name sessions and capture
    <code>/rename</code>. Warning: your prompts will be stored in clear text.</div>`
      : ""
  }
  ${
    prompts_text
      ? `<div class=note>Prompt content enabled &middot;
    ${escapeHtml(formatNumber(slash_seen))} slash commands seen, incl. ${escapeHtml(formatNumber(renames))} <code>/rename</code>.
    ${slashNote}
    </div>`
      : ""
  }
  ${
    unknown
      ? `<div class="note err">${escapeHtml(formatNumber(unknown))} unrecognized events:
    the format may have changed on the Claude Code side.</div>`
      : ""
  }
  <div class=two>
    <div class=box style=margin-top:0><h2>Ingestion stream</h2><p class=cap>&nbsp;</p>
      ${ingestTable(ingest)}</div>
    <div class=box style=margin-top:0><h2>Metrics received</h2><p class=cap>&nbsp;</p>
      ${metricNameTable(metric_names)}</div>
  </div>
  ${
    idle?.length
      ? `<div class=box><h2>Idle sessions</h2><p class=cap>Came up, registered their hooks
    and spent nothing &middot; left out of every count and median, on this page only
    &middot; click a session identifier to open it, on a screen wide enough to show
    that column</p>
    ${idleTable(idle)}</div>`
      : ""
  }
  ${
    hooks?.length
      ? `<div class=box><h2>Hooks</h2><p class=cap>Latency overhead and failures per hook
    &middot; click a row for its fires</p>
    ${hookTable(hooks)}</div>`
      : `<div class=note>No hook activity recorded yet &mdash; it appears once your configured
    hooks (PreToolUse / PostToolUse / UserPromptSubmit&hellip;) run.</div>`
  }
  <div class=box><h2>Events received</h2><p class=cap>&nbsp;</p>
    ${eventNameTable(event_names)}</div>
  ${
    notes.length
      ? `<div class=box><h2>Ingestion errors</h2><p class=cap>&nbsp;</p>
    ${noteTable(notes)}</div>`
      : ""
  }
  <div class=box><h2>Database</h2><p class=cap>Query directly in SQL</p>
    <code>${escapeHtml(db)}</code></div>`;
};

export { pages };
