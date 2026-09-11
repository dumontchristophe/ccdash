import { escapeHtml, formatDateTime, formatNumber } from "../format.mjs";
import { bytesCell, originCols, renderTable } from "../components.mjs";

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

export { callsModal };
