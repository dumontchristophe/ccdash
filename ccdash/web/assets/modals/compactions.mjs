import { escapeHtml, formatDateTime, formatNumber } from "../format.mjs";

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

export { compactionsModal };
