import { pager, sort } from "./state.mjs";
import { escapeHtml, estTokens, formatBytes, formatDateTime, formatNumber } from "./format.mjs";

// A window of "All" over a long history would build five thousand rows of DOM.
const ROWS_PER_PAGE = 50;

// `id` keys the current page in `pager`, and comes back on a click.
function paginate(id, items, size = ROWS_PER_PAGE) {
  const pageCount = Math.ceil(items.length / size);
  // Clamped: narrowing a filter leaves the stored index past the end.
  const current = Math.max(0, Math.min(pager[id] || 0, pageCount - 1));
  const visible = items.slice(current * size, (current + 1) * size);
  if (pageCount < 2) {
    return { visible, control: "" };
  }
  // A select, not text: the arrows alone take 200 clicks to cross a session.
  const options = Array.from(
    { length: pageCount },
    (_, i) =>
      `<option value="${i}"${i === current ? " selected" : ""}>Page ${i + 1} / ${pageCount}</option>`,
  ).join("");
  const control = `<div class=pager><button data-pager="${id}" data-page="${current - 1}" ${
    current ? "" : "disabled"
  }>&larr;</button>
  <select data-pager="${id}" aria-label="Page">${options}</select>
  <button data-pager="${id}" data-page="${current + 1}" ${
    current + 1 < pageCount ? "" : "disabled"
  }>&rarr;</button>
  <span>${escapeHtml(formatNumber(items.length))} rows</span></div>`;
  return { visible, control };
}

// A column mixing numbers and strings is a backend defect, not a case handled here.
const compareOn = (key, a, b) => {
  const x = a[key];
  const y = b[key];
  return typeof x === "number" ? x - y : String(x ?? "").localeCompare(String(y ?? ""));
};

// Render a sortable table.
//   id       - unique id; its sort state is remembered in `sort[id]`
//   cols     - { key, header, cell(row), cls?(row), hide?: "max-md" }
//   data     - array of row objects
//   key      - optional row field exposed as data-id (makes the row clickable)
//   first    - column sorted on until a header is clicked, defaulting to column
//              1. It has to be one a phone still shows: a hidden header cannot
//              be clicked, so the order would read as arbitrary and nothing
//              else could be sorted. Enforced by
//              `test_the_default_sort_never_names_a_hidden_column`.
//   tiebreak - optional second sort key, same direction, when `first` ties.
// Written out rather than interpolated: the Tailwind scanner reads this file as
// text and cannot see a class name a template only builds at runtime.
const hideClass = (col) => (col.hide === "max-md" ? " max-md:hidden" : "");

function renderTable(id, cols, data, key, first, tiebreak) {
  const activeSort = sort[id] || { k: first || cols[1]?.key, d: -1 };

  const sortedRows = [...data].sort((a, b) => {
    const cmp = compareOn(activeSort.k, a, b) * activeSort.d;
    if (cmp || !tiebreak) return cmp;
    return compareOn(tiebreak, a, b) * activeSort.d;
  });

  const header = cols
    .map((col) => {
      // "s" marks the sorted column, " a" adds the ascending arrow.
      const arrow = activeSort.d > 0 ? " a" : "";
      const sortClass = activeSort.k === col.key ? "s" + arrow : "";
      return `<th data-k="${escapeHtml(col.key)}" class="${sortClass}${hideClass(col)}">${escapeHtml(col.header)}</th>`;
    })
    .join("");

  // Paging after sorting, so a column ranks the whole list, not one slice.
  const { visible, control } = paginate(id, sortedRows);

  const rows = visible.length
    ? visible
        .map((row) => {
          const rowAttr = key ? `data-id="${escapeHtml(row[key])}"` : "";
          const cells = cols
            .map(
              (col) =>
                `<td class="${col.cls ? escapeHtml(col.cls(row)) : ""}${hideClass(col)}">${col.cell(row)}</td>`,
            )
            .join("");
          return `<tr ${rowAttr}>${cells}</tr>`;
        })
        .join("")
    : `<tr><td colspan=${escapeHtml(cols.length)} class="empty">No data</td></tr>`;

  // The wrapper scrolls sideways; sorting and clicks reach the table by closest().
  return `<div class=tw><table data-t=${id}><thead><tr>${header}</tr></thead>
    <tbody>${rows}</tbody></table></div>${control}`;
}
const renderTabs = (name, cur, items) =>
  `<div class=tabs>${items
    .map(
      ([k, l, n]) =>
        `<button data-tab=${name} data-v=${k} class="${cur === k ? "on" : ""}">${l}${
          n == null ? "" : `<b>${n}</b>`
        }</button>`,
    )
    .join("")}</div>`;

// Date + time: a session can span several days, so the time alone is ambiguous.
const whenCol = (key) => ({
  key,
  header: "When",
  cell: (row) => escapeHtml(formatDateTime(row[key])),
  cls: () => "num dim",
});
// One site, so the escaping is not a per-column decision.
const numCell = (field) => (row) => escapeHtml(formatNumber(row[field]));
// One site for the pair, so the token line cannot name another field than the
// size above it. `estTokens` goes in raw: it is the one formatter returning markup.
const bytesCell = (field) => (row) =>
  `${escapeHtml(formatBytes(row[field]))}<div class=sub>${estTokens(row[field])} tok</div>`;

// Global tables only. `data-goto` is caught by the click handler before the modal.
const originCols = (isGlobal) =>
  isGlobal
    ? [
        {
          key: "project",
          header: "Project",
          cell: (row) => `<span class=tag>${escapeHtml(row.project || "(undefined)")}</span>`,
        },
        {
          key: "session_id",
          hide: "max-md",
          header: "Session",
          cell: (row) =>
            row.session_id
              ? `<span class=slink data-goto="session/${escapeHtml(row.session_id)}">${escapeHtml(
                  row.session_id.slice(0, 8),
                )}</span>`
              : "-",
        },
      ]
    : [];

// A KPI tile. `label`, `value` and `hint` are payload-derived and escaped here;
// `hintTail` is the markup slot, appended raw for a caller's own tag or span.
const statCard = (label, value, hint, icon, cls = "", hintTail = "") => {
  const foot = escapeHtml(hint) + hintTail;
  return `<div class="card ${cls}"><div class=ico>${icon || ""}</div>
  <div class=l>${escapeHtml(label)}</div><div class=v>${escapeHtml(value)}</div><div class=h>${
    foot || "&nbsp;"
  }</div></div>`;
};

export { renderTable, renderTabs, whenCol, numCell, bytesCell, originCols, statCard, paginate };
