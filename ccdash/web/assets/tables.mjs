import { escapeHtml, formatBytes, formatDateTime, formatDuration, formatMoney } from "./format.mjs";
import { bytesCell, numCell, originCols, renderTable, whenCol } from "./components.mjs";

// Column definitions handed to renderTable, so sorting and paging stay in
// components.mjs. A diagnostics table carries its own id, rendering once on one
// page; an analysis table takes one, being drawn under two scopes at once.

const toolTable = (id, rows) =>
  renderTable(
    id,
    [
      {
        key: "label",
        header: "Tool",
        cell: (row) =>
          `${escapeHtml(row.label)} <span class="tag ${escapeHtml(row.kind)}">${escapeHtml(row.kind)}</span>`,
      },
      {
        key: "calls",
        header: "Calls",
        cell: numCell("calls"),
        cls: () => "num",
      },
      {
        key: "failures",
        header: "Failures",
        cell: numCell("failures"),
        cls: (row) => (row.failures ? "num ko" : "num dim"),
      },
      {
        key: "median_bytes",
        hide: "max-md",
        header: "Median",
        cell: (row) => escapeHtml(formatBytes(row.median_bytes)),
        cls: () => "num",
      },
      {
        key: "p95",
        hide: "max-md",
        header: "p95",
        cell: (row) => escapeHtml(formatBytes(row.p95)),
        cls: () => "num",
      },
      {
        key: "total_bytes",
        header: "Total",
        cell: bytesCell("total_bytes"),
        cls: () => "num",
      },
      {
        key: "share",
        hide: "max-md",
        header: "Size share",
        cell: (row) => `${row.share.toFixed(1)}%
    <div class=mini><i style="width:${row.share.toFixed(1)}%"></i></div>`,
        cls: () => "num",
      },
      {
        key: "avg_duration_ms",
        hide: "max-md",
        header: "Avg. duration",
        cell: (row) =>
          row.avg_duration_ms ? escapeHtml(Math.round(row.avg_duration_ms)) + " ms" : "-",
        cls: () => "num dim",
      },
    ],
    rows,
    "label",
  );

// The name leads and the directory follows: paths within a session share a long
// prefix, and only the last segment tells them apart.
const fileTable = (id, rows) =>
  renderTable(
    id,
    [
      {
        key: "file_path",
        header: "File",
        cell: (row) => `${escapeHtml(row.file_path.split("/").pop())}
    <div class=sub title="${escapeHtml(row.file_path)}" style="max-width:min(320px, 60vw);overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap">${escapeHtml(
      row.file_path.includes("/") ? row.file_path.slice(0, row.file_path.lastIndexOf("/")) : "",
    )}</div>`,
      },
      {
        key: "edits",
        header: "Edits",
        cell: numCell("edits"),
        cls: () => "num",
      },
      {
        key: "writes",
        hide: "max-md",
        header: "Writes",
        cell: numCell("writes"),
        cls: () => "num",
      },
      {
        key: "failures",
        header: "Failures",
        cell: numCell("failures"),
        cls: (row) => (row.failures ? "num ko" : "num dim"),
      },
      { ...whenCol("last"), hide: "max-md" },
    ],
    rows,
    "file_path",
    "edits",
  );

const bashTable = (id, rows, isGlobal) =>
  renderTable(
    id,
    [
      {
        key: "desc",
        header: "Description",
        cell: (row) => `<span title="${escapeHtml(row.cmd)}"
    style="display:inline-block;max-width:min(520px, 60vw);overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;vertical-align:bottom">${escapeHtml(row.desc)}</span>`,
      },
      { ...whenCol("ts"), hide: "max-md" },
      ...originCols(isGlobal),
      {
        // Visible on a phone: slowest first is the ranking this table is read for.
        key: "duration_ms",
        header: "Duration",
        cell: (row) => (row.duration_ms ? escapeHtml(Math.round(row.duration_ms)) + " ms" : "-"),
        cls: () => "num dim",
      },
      {
        key: "bytes",
        hide: "max-md",
        header: "Result size",
        cell: bytesCell("bytes"),
        cls: () => "num",
      },
      {
        key: "success",
        header: "Status",
        cell: (row) => (row.success === false ? `✗ ${escapeHtml(row.error_type || "")}` : "✓"),
        cls: (row) => (row.success === false ? "ko" : "dim"),
      },
    ],
    rows,
    "id",
    "duration_ms",
  );

const promptTable = (id, rows, isGlobal) =>
  renderTable(
    id,
    [
      whenCol("started_at"),
      {
        key: "prompt_text",
        header: "Prompt",
        cell: (row) => `<span title="${escapeHtml(row.prompt_text)}" style="display:inline-block;
    max-width:min(430px, 60vw);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    vertical-align:bottom">${escapeHtml(row.prompt_text || "(redacted)")}</span>`,
        cls: (row) => (row.prompt_text ? "" : "dim"),
      },
      ...originCols(isGlobal),
      {
        key: "tools",
        hide: "max-md",
        header: "Tools",
        cell: numCell("tools"),
        cls: () => "num",
      },
      {
        key: "bytes",
        hide: "max-md",
        header: "Result size",
        cell: bytesCell("bytes"),
        cls: () => "num",
      },
      {
        key: "failures",
        hide: "max-md",
        header: "Failures",
        cell: numCell("failures"),
        cls: (row) => (row.failures ? "num ko" : "num dim"),
      },
      {
        key: "compactions",
        hide: "max-md",
        header: "Compact.",
        cell: numCell("compactions"),
        cls: (row) => (row.compactions ? "num amber" : "num dim"),
      },
      {
        key: "cost",
        header: "Cost",
        cell: (row) => (row.cost ? escapeHtml(formatMoney(row.cost)) : "-"),
        cls: () => "num",
      },
      {
        key: "duration_s",
        hide: "max-md",
        header: "Duration",
        cell: (row) => escapeHtml(formatDuration(row.duration_s)),
        cls: () => "num dim",
      },
    ],
    rows,
    "prompt_id",
    "started_at",
  );

const invTable = (id, skills, mcp) =>
  renderTable(
    id,
    [
      { key: "name", header: "Item", cell: (row) => escapeHtml(row.name) },
      {
        key: "type",
        header: "Type",
        cell: (row) => `<span class="tag ${escapeHtml(row.type)}">${escapeHtml(row.type)}</span>`,
      },
      {
        key: "uses",
        header: "Count",
        cell: numCell("uses"),
        cls: () => "num",
      },
    ],
    [
      ...skills.map((row) => ({ ...row, type: "Skill" })),
      ...mcp.map((row) => ({ ...row, type: "MCP" })),
    ],
  );

const subagentTable = (id, rows, isGlobal) =>
  renderTable(
    id,
    [
      whenCol("ts"),
      {
        key: "agent_type",
        header: "Type",
        cell: (row) => `<span class="tag Agent">${escapeHtml(row.agent_type || "?")}</span>`,
      },
      ...originCols(isGlobal),
      {
        key: "model",
        hide: "max-md",
        header: "Model",
        cell: (row) =>
          row.model
            ? `<span class="tag ${escapeHtml(row.model)}">${escapeHtml(row.model)}</span>`
            : "-",
      },
      {
        key: "tokens",
        header: "Tokens",
        cell: numCell("tokens"),
        cls: () => "num",
      },
      {
        key: "tools",
        hide: "max-md",
        header: "Tool uses",
        cell: numCell("tools"),
        cls: () => "num",
      },
      {
        key: "duration_ms",
        hide: "max-md",
        header: "Duration",
        cell: (row) => (row.duration_ms ? escapeHtml(formatDuration(row.duration_ms / 1000)) : "-"),
        cls: () => "num dim",
      },
    ],
    rows,
    "id",
    "ts",
  );

const errTable = (id, rows, isGlobal) =>
  renderTable(
    id,
    [
      { ...whenCol("ts"), hide: "max-md" },
      {
        key: "label",
        header: "Tool",
        cell: (row) => escapeHtml(row.label || row.tool_name || "?"),
      },
      ...originCols(isGlobal),
      {
        key: "error_type",
        header: "Error",
        cell: (row) => escapeHtml(row.error_type || "?"),
        cls: () => "ko",
      },
      {
        // Keyed on the command: every shell failure carries the same fixed
        // message, so only the command groups the repeats.
        key: "cmd",
        header: "What failed",
        cell: (row) => `<span title="${escapeHtml(row.cmd || row.msg || "")}"
    style="display:inline-block;max-width:min(420px, 60vw);overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;vertical-align:bottom">${escapeHtml(row.cmd || row.msg || "—")}</span>`,
        cls: () => "dim",
      },
    ],
    rows,
    "id",
    "error_type",
  );

// Keyed on the event name, a payload value, so a Map and not an object literal.
// An `api_retries_exhausted` is the orphan the backend ships alone: no status and
// no message, which the cell says rather than leaving to three dashes.
const EXHAUSTED_LABELS = new Map([
  ["api_error", "Retries exhausted"],
  ["api_retries_exhausted", "Retries exhausted, no error reported"],
]);

// One row per incident, so an exhausted retry chain marks the error it closes
// instead of doubling it.
const apiErrTable = (id, rows, isGlobal) =>
  renderTable(
    id,
    [
      { ...whenCol("ts"), hide: "max-md" },
      {
        key: "exhausted",
        header: "Kind",
        cell: (row) =>
          row.exhausted
            ? `<span class=amber>${escapeHtml(EXHAUSTED_LABELS.get(row.name) || "Retries exhausted")}</span>`
            : "",
      },
      ...originCols(isGlobal),
      {
        key: "model",
        hide: "max-md",
        header: "Model",
        cell: (row) =>
          row.model
            ? `<span class="tag ${escapeHtml(row.model)}">${escapeHtml(row.model)}</span>`
            : "-",
      },
      {
        key: "status_code",
        header: "Status",
        cell: (row) => (row.status_code == null ? "-" : escapeHtml(String(row.status_code))),
        cls: () => "ko",
      },
      {
        key: "error",
        header: "Message",
        cell: (row) => `<span title="${escapeHtml(row.error || "")}"
    style="display:inline-block;max-width:min(420px, 60vw);overflow:hidden;text-overflow:ellipsis;
    white-space:nowrap;vertical-align:bottom">${escapeHtml(row.error || "—")}</span>`,
        cls: () => "dim",
      },
      {
        key: "attempts",
        header: "Attempts",
        cell: (row) => escapeHtml(row.attempts || "-"),
        cls: () => "num",
      },
      {
        key: "duration_ms",
        header: "Duration",
        // The whole retry chain when there was one: that is the wait it cost.
        cell: (row) => (row.duration_ms ? escapeHtml(formatDuration(row.duration_ms / 1000)) : "-"),
        cls: () => "num dim",
      },
    ],
    rows,
    "id",
    "attempts",
  );

const decTable = (id, rows) =>
  renderTable(
    id,
    [
      { key: "tool_name", header: "Tool", cell: (row) => escapeHtml(row.tool_name || "?") },
      {
        key: "decision",
        header: "Decision",
        cell: (row) => escapeHtml(row.decision || "?"),
        cls: (row) => (row.decision === "reject" ? "ko" : ""),
      },
      {
        key: "dec_source",
        header: "Source",
        cell: (row) => escapeHtml(row.dec_source || "?"),
        cls: () => "dim",
      },
      {
        key: "decisions",
        header: "Count",
        cell: numCell("decisions"),
        cls: () => "num",
      },
    ],
    rows,
  );

// One row per ingestion endpoint, ranked on what it refused.
const ingestTable = (rows) =>
  renderTable(
    "ing",
    [
      // The request path verbatim, including one that matched no route at all.
      { key: "kind", header: "Stream", cell: (row) => escapeHtml(row.kind) },
      {
        key: "batches",
        hide: "max-md",
        header: "Batches",
        cell: numCell("batches"),
        cls: () => "num",
      },
      {
        key: "accepted",
        header: "Accepted",
        cell: numCell("accepted"),
        cls: () => "num",
      },
      {
        key: "skipped",
        header: "Skipped",
        cell: numCell("skipped"),
        cls: (row) => (row.skipped ? "num ko" : "num dim"),
      },
      {
        key: "last",
        hide: "max-md",
        header: "Last",
        cell: (row) => escapeHtml(formatDateTime(row.last)),
        cls: () => "num dim",
      },
    ],
    rows,
    null,
    "skipped",
  );

// The metric names received, shorn of the `claude_code.` prefix they all carry.
const metricNameTable = (rows) =>
  renderTable(
    "mn",
    [
      {
        key: "name",
        header: "Name",
        cell: (row) => `<span class=num>${escapeHtml(row.name.replace("claude_code.", ""))}</span>`,
      },
      {
        key: "points",
        header: "Points",
        cell: numCell("points"),
        cls: () => "num",
      },
    ],
    rows,
  );

// Sessions that came up and spent nothing.
const idleTable = (rows) =>
  renderTable(
    "idl",
    [
      ...originCols(true),
      whenCol("started_at"),
      {
        key: "points",
        header: "Points",
        cell: numCell("points"),
        cls: () => "num dim",
      },
    ],
    rows,
    null,
    "started_at",
  );

// Latency overhead and failures per hook; a click lists a hook's fires.
const hookTable = (rows) =>
  renderTable(
    "hk",
    [
      {
        key: "name",
        header: "Hook",
        cell: (row) => `<span class=num>${escapeHtml(row.name)}</span>`,
      },
      {
        key: "event",
        hide: "max-md",
        header: "Event",
        cell: (row) => escapeHtml(row.event || "-"),
        cls: () => "dim",
      },
      {
        key: "fires",
        header: "Fires",
        cell: numCell("fires"),
        cls: () => "num",
      },
      {
        key: "avg",
        hide: "max-md",
        header: "Avg",
        cell: (row) => escapeHtml(Math.round(row.avg)) + " ms",
        cls: () => "num",
      },
      {
        key: "max",
        hide: "max-md",
        header: "Max",
        cell: (row) => escapeHtml(Math.round(row.max)) + " ms",
        cls: () => "num dim",
      },
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
    rows,
    "name",
    "fires",
  );

// `unknown` is how a format change on the Claude Code side shows up, hence the red.
const eventNameTable = (rows) =>
  renderTable(
    "en",
    [
      {
        key: "name",
        header: "Type",
        cell: (row) => `<span class=num>${escapeHtml(row.name)}</span>`,
        cls: (row) => (row.name === "unknown" ? "ko" : ""),
      },
      {
        key: "points",
        header: "Count",
        cell: numCell("points"),
        cls: () => "num",
      },
    ],
    rows,
  );

// What ingestion rejected, one row per distinct message.
const noteTable = (rows) =>
  renderTable(
    "nt",
    [
      {
        key: "note",
        header: "Message",
        cell: (row) => `<span class=num>${escapeHtml(row.note)}</span>`,
      },
      {
        key: "batches",
        header: "Count",
        cell: numCell("batches"),
        cls: () => "num ko",
      },
      {
        key: "last",
        header: "Last",
        cell: (row) => escapeHtml(formatDateTime(row.last)),
        cls: () => "num dim",
      },
    ],
    rows,
  );

export {
  toolTable,
  fileTable,
  bashTable,
  promptTable,
  invTable,
  subagentTable,
  errTable,
  apiErrTable,
  decTable,
  ingestTable,
  metricNameTable,
  idleTable,
  hookTable,
  eventNameTable,
  noteTable,
};
