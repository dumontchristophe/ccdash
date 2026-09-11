import {
  escapeHtml,
  formatDateTime,
  formatDuration,
  formatMoney,
  formatNumber,
} from "../format.mjs";
import { modalBox, numCell, renderTable, statCard } from "../components.mjs";
import { subagentTable, toolTable } from "../tables.mjs";

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
  return modalBox({
    title: "Prompt",
    cap: `${escapeHtml(formatDateTime(d.ts))} &middot; ${origin}`,
    body: `${
      d.prompt_text
        ? `<div class="snippet max-h-[22vh] overflow-auto">${escapeHtml(d.prompt_text)}</div>`
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
  }`,
  });
};

export { promptDetail };
