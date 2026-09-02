import {
  escapeHtml,
  estTokens,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatMoney,
  formatNumber,
} from "./format.mjs";
import { bytesCell, numCell, originCols, renderTable, statCard } from "./components.mjs";
import { subagentTable, toolTable } from "./tables.mjs";

// The overlays opened from a table row, and the only place a stored payload is
// shown in full.

// The turn a record belongs to, opened on top of the current modal.
const promptLink = (id, label) =>
  id
    ? `<span class=slink data-prompt="${escapeHtml(id)}">${escapeHtml(label || "prompt")}</span>`
    : "";

// `prompt_id` is the exception: it addresses a modal, so it renders as the link.
const inspectorValue = (k, v) => {
  if (k === "prompt_id") return promptLink(v, v);
  if (typeof v === "object") return escapeHtml(JSON.stringify(v, null, 1));
  return escapeHtml(v);
};

// Every field the event holds, as the ingester stored it. `max-md:flex-wrap`:
// `.n` reserves 180px of a 281px box, leaving a JSON value 88px to break into.
// `display:block` opts out of the `.tl .e .d` clamp -- data, not a caption.
const rawAttrs = (e) => `<div class=tl>${Object.entries(e)
  .filter(([k, v]) => v !== null && v !== "")
  .map(
    ([k, v]) => `<div class="e max-md:flex-wrap"><span class=n>${escapeHtml(k)}</span>
    <span class=d style="display:block;white-space:pre-wrap;word-break:break-word">${inspectorValue(
      k,
      v,
    )}</span></div>`,
  )
  .join("")}
  </div>`;

// <details> keeps its own open state and takes no handler, so it costs no re-render.
const rawDetails = (e) => `<details style="margin-top:16px">
  <summary class=cap style=cursor:pointer>Raw attributes</summary>
  <div style="max-height:40vh;overflow:auto">${rawAttrs(e)}</div></details>`;

const inspector = (e) => `<div class=box><h2>Raw attributes</h2>
  <p class=cap>Event ${escapeHtml(e.id)} &middot; ${escapeHtml(e.name)}
    <button data-close style=float:right>close</button></p>
  ${rawAttrs(e)}</div>`;

// `d` is /api/subagent. Every card grid of this file reads 140px: `auto-fit`
// collapses to one column as soon as two no longer fit, and under that a label
// gets ~59px of a 281px modal box once `.ico` has taken its 34px.
const subagentDetail = (d) => `<div class=box><h2>Sub-agent</h2>
  <p class=cap><span class="tag Agent">${escapeHtml(d.agent_type || "?")}</span>
    ${d.model ? `<span class="tag ${escapeHtml(d.model)}">${escapeHtml(d.model)}</span>` : ""}
    ${d.background ? `<span class=tag>background</span>` : ""}
    ${d.isolation ? `<span class=tag>${escapeHtml(d.isolation)}</span>` : ""}
    ${(d.efforts || []).map((e) => `<span class=tag>${escapeHtml(e)}</span>`).join("")}
    <button data-close style=float:right>close</button></p>
  <div class=cards style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
    ${statCard("Tokens", formatNumber(d.tokens), "", "∿")}
    ${statCard("Tool uses", formatNumber(d.tools), "", "⚒")}
    ${statCard("Duration", d.duration_ms ? formatDuration(d.duration_ms / 1000) : "-", "", "◴")}
  </div>
  ${d.description ? `<h3 style="font-size:14px;margin:16px 0 6px">${escapeHtml(d.description)}</h3>` : ""}
  ${
    d.instructions
      ? `<div style="max-height:52vh;overflow:auto;white-space:pre-wrap;word-break:break-word;
    font:12.5px var(--fn);background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(
      d.instructions,
    )}</div>`
      : `<p class=cap>Instructions unavailable (the spawning call was not captured).
    The internal tools it ran are not attributable in the telemetry.</p>`
  }</div>`;

// Everything one prompt set off, from /api/prompt. The tools and sub-agents tables
// reuse the global renderers, so their rows keep their drill-downs (handleRowClick).
const promptDetail = (d) => {
  const origin = [
    d.project ? `<span class=tag>${escapeHtml(d.project)}</span>` : "",
    d.session_id
      ? `<span class=slink data-goto="session/${escapeHtml(d.session_id)}">${escapeHtml(
          d.session_id.slice(0, 8),
        )}</span>`
      : "",
  ]
    .filter(Boolean)
    .join(" &middot; ");
  // Fresh input against cache read: a turn re-sending its context shows a huge ratio.
  const tokens = [
    `input ${escapeHtml(formatNumber(d.input_tokens))}`,
    `output ${escapeHtml(formatNumber(d.output_tokens))}`,
    `cache read ${escapeHtml(formatNumber(d.cache_read_tokens))}`,
    `cache write ${escapeHtml(formatNumber(d.cache_creation_tokens))}`,
  ].join(" &middot; ");
  const section = (title, body) =>
    `<h3 style="font-size:13px;margin:20px 0 6px">${title}</h3>${body}`;
  return `<div class=box><h2>Prompt</h2>
  <p class=cap>${escapeHtml(formatDateTime(d.ts))} &middot; ${origin}
    <button data-close style=float:right>close</button></p>
  ${
    d.prompt_text
      ? `<div style="max-height:22vh;overflow:auto;white-space:pre-wrap;word-break:break-word;
    font:12.5px var(--fn);background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(
      d.prompt_text,
    )}</div>`
      : `<p class=cap>Prompt text not recorded (needs <code>OTEL_LOG_USER_PROMPTS=1</code>).</p>`
  }
  <div class=cards style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:16px">
    ${statCard("Est. cost", formatMoney(d.cost), d.calls + " model calls", "◎")}
    ${statCard("Duration", formatDuration(d.duration_s), "", "◴")}
    ${statCard("Hook overhead", d.hook_ms ? Math.round(d.hook_ms) + " ms" : "-", "before the tools ran", "⚙")}
    ${statCard("Output tokens", formatNumber(d.output_tokens), "", "∿")}
  </div>
  <p class=cap style="margin:14px 0 0">Tokens &mdash; ${tokens}</p>
  ${
    d.toolstats.length
      ? section(
          "Tools <span class=cap>&middot; click one for its calls in this turn</span>",
          toolTable("ptools", d.toolstats),
        )
      : ""
  }
  ${
    d.subagents.length
      ? section(
          "Sub-agents <span class=cap>&middot; click one for what it reported</span>",
          subagentTable("psubc", d.subagents, false),
        )
      : ""
  }
  ${
    d.hooks.length
      ? section(
          "Hooks",
          // No row key, so no click: the hook detail is global and would answer
          // about every project at once.
          renderTable(
            "phk",
            [
              { key: "name", header: "Hook", cell: (row) => escapeHtml(row.name) },
              {
                key: "fires",
                header: "Fires",
                cell: numCell("fires"),
                cls: () => "num",
              },
              {
                key: "ms",
                header: "Total",
                cell: (row) => escapeHtml(Math.round(row.ms)) + " ms",
                cls: () => "num dim",
              },
            ],
            d.hooks,
          ),
        )
      : ""
  }</div>`;
};

const bashDetail = (e) => {
  const p = e.params || {};
  // The source (settings, prompt…) in parentheses, when the event carries it.
  const source = e.dec_source ? " (" + escapeHtml(e.dec_source) + ")" : "";
  const decisionClass = e.decision === "reject" ? "ko" : "dim";
  const decision = e.decision
    ? ` &middot; <span class="${decisionClass}">${escapeHtml(e.decision)}${source}</span>`
    : "";
  return `<div class=box><h2>Bash call</h2>
  <p class=cap>${escapeHtml(formatDateTime(e.ts))}${decision}${
    e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""
  }<button data-close style=float:right>close</button></p>
  ${p.description ? `<h3 style="font-size:14px;margin:8px 0 10px">${escapeHtml(p.description)}</h3>` : ""}
  <div style="white-space:pre-wrap;word-break:break-word;font:12.5px var(--fn);
    background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(
      p.full_command || e.bash_cmd || "",
    )}</div>
  <div class=cards style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-top:14px">
    ${statCard("Duration", e.duration_ms ? Math.round(e.duration_ms) + " ms" : "-", "", "◴")}
    ${statCard("Result size", formatBytes(e.result_bytes), "", "▤", "", `${estTokens(e.result_bytes)} tok`)}
    ${statCard(
      "Status",
      e.success === false ? "failed" : "ok",
      e.error_type || "",
      "",
      e.success === false ? "hl" : "",
    )}
  </div>${rawDetails(e)}</div>`;
};

// `e` is an /api/event payload, attrs and params already parsed.
const errorDetail = (e) => {
  const p = e.params || {};
  const attempted = p.full_command || (Object.keys(p).length ? JSON.stringify(p, null, 1) : "");
  return `<div class=box><h2>Failed ${escapeHtml(e.tool_name || "call")}</h2>
  <p class=cap>${escapeHtml(formatDateTime(e.ts))} &middot; <span class=ko>${escapeHtml(
    e.error_type || "error",
  )}</span>${
    e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""
  }<button data-close style=float:right>close</button></p>
  ${e.file_path ? `<p class=cap style="margin:6px 0 0">${escapeHtml(e.file_path)}</p>` : ""}
  ${e.attrs?.error ? `<div class="note err">${escapeHtml(e.attrs.error)}</div>` : ""}
  ${
    attempted
      ? `<h3 style="font-size:13px;margin:14px 0 6px">Attempted</h3>
  <div style="white-space:pre-wrap;word-break:break-word;font:12.5px var(--fn);
    background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(attempted)}</div>`
      : ""
  }${rawDetails(e)}</div>`;
};

// Claude Code clips a string over 512 characters to its first 128 and appends the
// original length (docs/reference.md); the marker is the only record of the cut.
const CLIPPED = /…\[(\d+) chars]$/;

// `tool_input` comes down parsed from api_event.
const editDetail = (e) => {
  const ti = e.tool_input || {};
  const pane = (title, body) => {
    const clipped = CLIPPED.exec(body || "");
    return `<h3 style="font-size:13px;margin:14px 0 6px">${title}${
      clipped ? ` <span class=cap>&middot; 128 of ${escapeHtml(clipped[1])} characters exported</span>` : ""
    }</h3>
  <div style="max-height:28vh;overflow:auto;white-space:pre-wrap;word-break:break-word;
    font:12.5px var(--fn);background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(
      body || "(empty)",
    )}</div>`;
  };
  const panes = ti.content
    ? pane("Content", ti.content)
    : pane("Before", ti.old_string) + pane("After", ti.new_string);
  return `<div class=box><h2>${escapeHtml(e.tool_name || "Edit")}</h2>
  <p class=cap>${escapeHtml(formatDateTime(e.ts))}${e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""}
    <button data-close style=float:right>close</button></p>
  <h3 style="font-size:14px;margin:8px 0 2px">${escapeHtml(
    (e.file_path || "?").split("/").pop(),
  )}</h3>
  <p class=cap style="margin:0">${escapeHtml(e.file_path || "")}</p>
  ${
    Object.keys(ti).length
      ? panes +
        `<p class=cap style="margin-top:10px">Claude Code clips each value before
    exporting it.</p>`
      : `<p class=cap style="margin-top:14px">Call arguments unavailable: Claude Code drops
    the whole attribute past 4096 characters.</p>`
  }${rawDetails(e)}</div>`;
};

// Type-aware detail for an /api/event payload; falls back to the raw inspector.
const detailView = (e) => {
  if (e.success === false) return errorDetail(e);
  if (e.tool_name === "Bash") return bashDetail(e);
  if (e.tool_name === "Edit" || e.tool_name === "Write") return editDetail(e);
  return inspector(e);
};

const hookDetail = (d) => `<div class=box><h2>Hook: ${escapeHtml(d.name)}</h2>
  <p class=cap>${escapeHtml(d.event || "")}${
    d.regs?.length
      ? " &middot; " +
        d.regs
          .map((row) => escapeHtml([row.source, row.type, row.matcher].filter(Boolean).join(" / ")))
          .join(", ")
      : ""
  }${
    // One name can chain several commands on a matcher, and the duration is their
    // total: without this, 40 ms reads as one script.
    d.hooks > 1 ? ` &middot; ${escapeHtml(formatNumber(d.hooks))} commands per fire` : ""
  }<button data-close style=float:right>close</button></p>
  ${renderTable(
    "hkfires",
    // Duration is column 2, which renderTable sorts on by default: the slowest
    // fires are what this modal exists to show.
    [
      {
        key: "ts",
        hide: "max-md",
        header: "When",
        cell: (row) => escapeHtml(formatDateTime(row.ts)),
        cls: () => "num dim",
      },
      {
        key: "duration_ms",
        header: "Duration",
        cell: (row) => escapeHtml(Math.round(row.duration_ms)) + " ms",
        cls: () => "num",
      },
      ...originCols(true),
      {
        key: "err",
        header: "Errors",
        cell: numCell("err"),
        cls: (row) => (row.err ? "num ko" : "num dim"),
      },
      {
        key: "block",
        header: "Blocks",
        cell: numCell("block"),
        cls: (row) => (row.block ? "num ko" : "num dim"),
      },
    ],
    d.fires,
  )}</div>`;

// Absent rather than empty for a tool that names no file: a drill-down is opened
// on one tool, so a column no row can fill is one that never fills.
const fileCol = (calls, shared) =>
  !shared && calls.some((row) => row.file_path)
    ? [
        {
          key: "file_path",
          header: "File",
          cell: (row) =>
            row.file_path
              ? `<span title="${escapeHtml(row.file_path)}">${escapeHtml(
                  row.file_path.split("/").pop(),
                )}</span>`
              : "-",
        },
      ]
    : [];

// Dropped when the modal is titled by that same tool. A file drill-down keeps it:
// that every change was an Edit is the answer, not a reason to drop the column.
const toolCol = (calls, label) =>
  calls.some((row) => row.label !== label)
    ? [{ key: "label", header: "Tool", cell: (row) => escapeHtml(row.label || "?") }]
    : [];

// `calls` is /api/calls; a row opens the event detail.
const callsModal = (label, calls) => {
  // One path on every row goes to the caption, written once and in full.
  const paths = new Set(calls.map((row) => row.file_path).filter(Boolean));
  const shared = paths.size === 1 && calls.every((row) => row.file_path);
  return `<div class=box><h2>${escapeHtml(label || "?")}</h2>
  <p class=cap>${escapeHtml(formatNumber(calls.length))} call${calls.length > 1 ? "s" : ""}${
    shared ? ` &middot; ${escapeHtml([...paths][0])}` : ""
  } &middot; click one for the raw event
    <button data-close style=float:right>close</button></p>
  ${renderTable(
    "acalls",
    [
      {
        key: "ts",
        header: "When",
        cell: (row) => escapeHtml(formatDateTime(row.ts)),
        cls: () => "num dim",
      },
      // A drill-down opened from a session detail already knows its origin.
      ...originCols(!location.hash.startsWith("#/session/")),
      ...toolCol(calls, label),
      ...fileCol(calls, shared),
      {
        key: "duration_ms",
        hide: "max-md",
        header: "Duration",
        cell: (row) => (row.duration_ms ? escapeHtml(Math.round(row.duration_ms)) + " ms" : "-"),
        cls: () => "num",
      },
      {
        key: "result_bytes",
        hide: "max-md",
        header: "Result size",
        cell: bytesCell("result_bytes"),
        cls: () => "num",
      },
      {
        key: "success",
        header: "Status",
        cell: (row) => (row.success === false ? `✗ ${escapeHtml(row.error_type || "")}` : "✓"),
        cls: (row) => (row.success === false ? "ko" : "dim"),
      },
    ],
    calls,
    "id",
    "ts",
  )}</div>`;
};

// The measured pair and the reported one describe different spans, so both are
// shown: the reported one alone reads as a context that fell further than it did.
const DASH = `<span class=dim>-</span>`;

const compactionPair = (a, b) =>
  a && b
    ? `${escapeHtml(formatNumber(a))} <span class=dim>&rarr;</span> ${escapeHtml(formatNumber(b))}`
    : DASH;

const compactionsModal = (compactions, context = []) => {
  // A class list, not an inline style, so a breakpoint can reach it: 242px of
  // fixed track in a 283px box would leave the two 1fr columns 20px each.
  const cols = "grid grid-cols-[132px_74px_1fr_1fr] gap-3 max-md:grid-cols-2";
  // Newest first. The payload is chronological because the curve reads it that
  // way, so the copy is reversed here rather than at the source.
  const rows = [...compactions]
    .reverse()
    .map((c) => {
      const before = context.findLast((p) => p.ts < c.ts);
      const after = context.find((p) => p.ts > c.ts);
      const trigger = escapeHtml(c.trigger_kind || "?");
      return `<div class="${cols}" style="padding:8px 0;border-bottom:1px solid var(--grid);
    align-items:center"><span class="num dim">${escapeHtml(formatDateTime(c.ts))}</span>
    <span><span class=tag>${trigger}</span></span>
    <span class=num>${compactionPair(before?.value, after?.value)}</span>
    <span class=num>${compactionPair(c.pre_tokens, c.post_tokens)}</span></div>`;
    })
    .join("");
  const heading = ["When", "Trigger", "Context measured", "Reported by Claude Code"]
    .map((h) => `<span class=dim style=font-size:11.5px>${h}</span>`)
    .join("");
  const body = rows || `<div class=empty>No compaction on this session.</div>`;
  return `<div class=box><h2>Compactions</h2>
  <p class=cap>${escapeHtml(formatNumber(compactions.length))} on this session &middot; <b>measured</b> is the
  prompt size of the requests either side, <b>reported</b> is what Claude Code
  declares for the span it summarised. The two do not describe the same thing.
  <button data-close style=float:right>close</button></p>
  <div class="${cols}" style="padding-bottom:7px;border-bottom:1px solid var(--line)">${heading}</div>
  ${body}</div>`;
};

export { detailView, subagentDetail, hookDetail, promptDetail, callsModal, compactionsModal };
