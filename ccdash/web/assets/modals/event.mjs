import { escapeHtml, estTokens, formatBytes, formatDateTime } from "../format.mjs";
import { modalBox, promptLink, statCard } from "../components.mjs";

// The event overlay: the only place a stored payload is shown in full, with a
// type-aware pane per tool and the raw inspector as the fallback.

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

const inspector = (e) =>
  modalBox({
    title: "Raw attributes",
    cap: `Event ${escapeHtml(e.id)} &middot; ${escapeHtml(e.name)}`,
    body: rawAttrs(e),
  });

const bashDetail = (e) => {
  const p = e.params || {};
  // The source (settings, prompt…) in parentheses, when the event carries it.
  const source = e.dec_source ? " (" + escapeHtml(e.dec_source) + ")" : "";
  const decisionClass = e.decision === "reject" ? "ko" : "dim";
  const decision = e.decision
    ? ` &middot; <span class="${decisionClass}">${escapeHtml(e.decision)}${source}</span>`
    : "";
  return modalBox({
    title: "Bash call",
    cap: `${escapeHtml(formatDateTime(e.ts))}${decision}${
      e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""
    }`,
    body: `${p.description ? `<h3 style="font-size:14px;margin:8px 0 10px">${escapeHtml(p.description)}</h3>` : ""}
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
  </div>${rawDetails(e)}`,
  });
};

// `e` is an /api/event payload, attrs and params already parsed.
const errorDetail = (e) => {
  const p = e.params || {};
  const attempted = p.full_command || (Object.keys(p).length ? JSON.stringify(p, null, 1) : "");
  return modalBox({
    title: `Failed ${escapeHtml(e.tool_name || "call")}`,
    cap: `${escapeHtml(formatDateTime(e.ts))} &middot; <span class=ko>${escapeHtml(
      e.error_type || "error",
    )}</span>${e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""}`,
    body: `${e.file_path ? `<p class=cap style="margin:6px 0 0">${escapeHtml(e.file_path)}</p>` : ""}
  ${e.attrs?.error ? `<div class="note err">${escapeHtml(e.attrs.error)}</div>` : ""}
  ${
    attempted
      ? `<h3 style="font-size:13px;margin:14px 0 6px">Attempted</h3>
  <div style="white-space:pre-wrap;word-break:break-word;font:12.5px var(--fn);
    background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(attempted)}</div>`
      : ""
  }${rawDetails(e)}`,
  });
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
      clipped
        ? ` <span class=cap>&middot; 128 of ${escapeHtml(clipped[1])} characters exported</span>`
        : ""
    }</h3>
  <div style="max-height:28vh;overflow:auto;white-space:pre-wrap;word-break:break-word;
    font:12.5px var(--fn);background:var(--card2);padding:12px;border-radius:8px">${escapeHtml(
      body || "(empty)",
    )}</div>`;
  };
  const panes = ti.content
    ? pane("Content", ti.content)
    : pane("Before", ti.old_string) + pane("After", ti.new_string);
  return modalBox({
    title: escapeHtml(e.tool_name || "Edit"),
    cap: `${escapeHtml(formatDateTime(e.ts))}${e.prompt_id ? ` &middot; ${promptLink(e.prompt_id)}` : ""}`,
    body: `<h3 style="font-size:14px;margin:8px 0 2px">${escapeHtml(
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
  }${rawDetails(e)}`,
  });
};

// Type-aware detail for an /api/event payload; falls back to the raw inspector.
const detailView = (e) => {
  if (e.success === false) return errorDetail(e);
  if (e.tool_name === "Bash") return bashDetail(e);
  if (e.tool_name === "Edit" || e.tool_name === "Write") return editDetail(e);
  return inspector(e);
};

export { detailView };
