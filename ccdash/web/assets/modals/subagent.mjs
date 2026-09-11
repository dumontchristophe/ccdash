import { escapeHtml, formatDuration, formatNumber } from "../format.mjs";
import { modalBox, statCard } from "../components.mjs";

// `d` is /api/subagent. Every card grid of the modals reads 140px: `auto-fit`
// collapses to one column as soon as two no longer fit, and under that a label
// gets ~59px of a 281px modal box once `.ico` has taken its 34px.
const subagentDetail = (d) =>
  modalBox({
    title: "Sub-agent",
    cap: `<span class="tag Agent">${escapeHtml(d.agent_type || "?")}</span>
    ${d.model ? `<span class="tag ${escapeHtml(d.model)}">${escapeHtml(d.model)}</span>` : ""}
    ${d.background ? `<span class=tag>background</span>` : ""}
    ${d.isolation ? `<span class=tag>${escapeHtml(d.isolation)}</span>` : ""}
    ${(d.efforts || []).map((e) => `<span class=tag>${escapeHtml(e)}</span>`).join("")}`,
    body: `<div class=cards style="grid-template-columns:repeat(auto-fit,minmax(140px,1fr))">
    ${statCard("Tokens", formatNumber(d.tokens), "", "∿")}
    ${statCard("Tool uses", formatNumber(d.tools), "", "⚒")}
    ${statCard("Duration", d.duration_ms ? formatDuration(d.duration_ms / 1000) : "-", "", "◴")}
  </div>
  ${d.description ? `<h3 style="font-size:14px;margin:16px 0 6px">${escapeHtml(d.description)}</h3>` : ""}
  ${
    d.instructions
      ? `<div class="snippet max-h-[52vh] overflow-auto">${escapeHtml(d.instructions)}</div>`
      : `<p class=cap>Instructions unavailable (the spawning call was not captured).
    The internal tools it ran are not attributable in the telemetry.</p>`
  }`,
  });

export { subagentDetail };
