import { escapeHtml, formatMoney, formatNumber } from "./format.mjs";
import { numCell, originCols, renderTable, renderTabs, statCard, whenCol } from "./components.mjs";
import { tab } from "./state.mjs";
import {
  toolTable,
  bashTable,
  promptTable,
  invTable,
  subagentTable,
  errTable,
  apiErrTable,
  decTable,
} from "./tables.mjs";

// A per-call list stops at a ceiling while the aggregates cover the whole window,
// so a list of the last 300 calls would otherwise read as every call of the month.
// `truncated` names the cut lists, on /api/analysis and /api/session alike.
const cutNote = (d, key, what) =>
  d.truncated?.includes(key)
    ? `<div class=note>Only the most recent ${what} are listed &mdash; the window holds
    more. Every count and median elsewhere still covers all of it.</div>`
    : "";

// Sort-state ids are namespaced by scope, so a global table and its session-detail
// twin do not share a sort.
const GLOBAL = "g";
const SESSION = "s";

// The three lists of Errors & Permissions, tabbed and not stacked: stacked, only
// the first gets read. A Map, since the key comes back from a DOM attribute.
const miscBodies = new Map([
  [
    "errd",
    (d, scope) =>
      d.errors.length
        ? `<p class=cap style="margin:0 0 8px">A shell failure reports no exit code and no
      stderr, so sort on <em>What failed</em> to group the repeats; click a row for the
      raw event</p>
      ${cutNote(d, "errors", "failures")}
      ${errTable(scope + "errd", d.errors, scope === GLOBAL)}`
        : `<div class=empty>No tool call failed here.</div>`,
  ],
  [
    "apierr",
    (d, scope) =>
      d.api_errors.length
        ? `<p class=cap style="margin:0 0 8px">One row per incident, not per event: a retry
      chain that ran out marks the error it closed instead of doubling it &middot; click a
      row for the raw event</p>
      ${cutNote(d, "api_errors", "incidents")}
      ${apiErrTable(scope + "apierr", d.api_errors, scope === GLOBAL)}`
        : `<div class=empty>No API error &mdash; every request Claude Code sent came back.
      This table only fills when Anthropic's API refuses a call or a retry chain runs
      out.</div>`,
  ],
  [
    "dec",
    (d, scope) =>
      d.decisions.length
        ? decTable(scope + "dec", d.decisions)
        : `<div class=empty>No permission decision recorded.</div>`,
  ],
]);

// Sub-tabs rendered under both scopes, each renderer taking (data, scope).
const analysisTabs = new Map([
  [
    "tools",
    (d, scope) =>
      (d.tools.length
        ? toolTable(scope + "tools", d.tools)
        : `<div class=empty>No tool calls</div>`) +
      (d.skills.length || d.mcp.length
        ? `<h3 style="font-size:13px;margin:22px 0 6px">
      Skills and MCP servers used</h3>${invTable(scope + "inv", d.skills, d.mcp)}`
        : `<p class=cap style=margin-top:18px>No skill or MCP server used.</p>`),
  ],
  [
    "bash",
    (d, scope) =>
      d.bash.length
        ? `${cutNote(d, "bash", "calls")}<p class=cap style="margin:0 0 10px">One row per call
      &middot; click for the full command</p>${bashTable(scope + "bashd", d.bash, scope === GLOBAL)}`
        : `<div class=empty>No Bash commands. Without <code>OTEL_LOG_TOOL_DETAILS=1</code>,
      commands are not sent.</div>`,
  ],
  [
    "prompts",
    (d, scope) =>
      d.prompts.length
        ? `${cutNote(d, "prompts", "turns")}${promptTable(scope + "prompts", d.prompts, scope === GLOBAL)}`
        : `<div class=empty>No prompts</div>`,
  ],
  // `subagent_completed` alone: a delegation still running reports no tokens.
  [
    "agents",
    (d, scope) =>
      d.subagents?.length
        ? `${cutNote(d, "subagents", "delegations")}<p class=cap style="margin:0 0 10px">Real
      tokens, tool uses and duration each delegation reported &middot; click a row for its
      instructions</p>${subagentTable(scope + "subc", d.subagents, scope === GLOBAL)}`
        : `<div class=empty>No completed sub-agent in this period. Delegations appear once a
      sub-agent finishes (needs <code>OTEL_LOG_TOOL_DETAILS=1</code>).</div>`,
  ],
  [
    "misc",
    (d, scope) => {
      const view = tab[scope + "misc"] || "errd";
      return (
        renderTabs(scope + "misc", view, [
          ["errd", "Failures", d.errors.length],
          ["apierr", "API errors", d.api_errors.length],
          ["dec", "Permissions", d.decisions.length],
        ]) +
        `<div class=tabbody>${(miscBodies.get(view) || miscBodies.get("errd"))(d, scope)}</div>`
      );
    },
  ],
  // Context pressure per session, from /api/context.
  [
    "context",
    (d, scope) => {
      if (!d.sessions.length) {
        return `<div class=empty>No session recorded yet.</div>`;
      }
      return `
    <div class=cards>
      ${statCard("Sessions", formatNumber(d.sessions.length), "in this period", "▭")}
      ${statCard("Auto compactions", formatNumber(d.auto_compactions), "context overflowed", "⇲")}
      ${statCard("Manual compactions", formatNumber(d.manual_compactions), "you asked for them", "⇲")}
      ${statCard("Peak context", formatNumber(d.pre_compaction_peak), "tokens before a compaction", "◔")}
    </div>
    <div class=box><h2>Context pressure by session</h2>
      <p class=cap>An <b>auto</b> compaction is the context overflowing on its own; a
      manual one is a choice. A dash under the peak means the session never compacted,
      not that it ran light &mdash; <b>Max context</b> is the column that says how heavy
      it was, measured on every main-thread request. It sits above the peak beside it
      and is meant to: Claude Code reports that one over the span it summarised, not
      over the prompt it sent.</p>
      ${renderTable(
        scope + "ctx",
        [
          ...originCols(scope === GLOBAL),
          {
            key: "auto_comp",
            header: "Auto compactions",
            cell: (row) => row.auto_comp || "0",
            cls: (row) => (row.auto_comp ? "num amber" : "num dim"),
          },
          {
            key: "man_comp",
            hide: "max-md",
            header: "Manual",
            cell: (row) => row.man_comp || "0",
            cls: () => "num dim",
          },
          {
            key: "pre_compaction_peak",
            hide: "max-md",
            header: "Peak before compacting",
            cell: (row) =>
              row.pre_compaction_peak
                ? escapeHtml(formatNumber(row.pre_compaction_peak))
                : "-",
            cls: () => "num",
          },
          {
            key: "max_context",
            header: "Max context",
            cell: (row) => (row.max_context ? escapeHtml(formatNumber(row.max_context)) : "-"),
            cls: () => "num",
          },
          {
            key: "cost",
            header: "Cost",
            cell: (row) => (row.cost ? escapeHtml(formatMoney(row.cost)) : "-"),
            cls: () => "num",
          },
          {
            key: "tools_per_prompt",
            hide: "max-md",
            header: "Tools per prompt",
            cell: (row) => (row.tools_per_prompt ? row.tools_per_prompt.toFixed(1) : "-"),
            cls: () => "num dim",
          },
          {
            key: "events",
            hide: "max-md",
            header: "Events",
            cell: numCell("events"),
            cls: () => "num dim",
          },
          { ...whenCol("ts"), hide: "max-md" },
        ],
        d.sessions,
        null,
        "auto_comp",
        // Where nothing compacted on its own, every session ties at zero.
        "pre_compaction_peak",
      )}</div>`;
    },
  ],
]);

export { analysisTabs, GLOBAL, SESSION };
