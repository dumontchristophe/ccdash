import { escapeHtml, formatDateTime, formatNumber } from "../format.mjs";
import { numCell, originCols, renderTable } from "../components.mjs";

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

export { hookDetail };
