import { escapeHtml, formatMoney, formatNumber, modelColor, TOKEN_TYPES } from "./format.mjs";

// Each function returns an inline <svg> string. W/h = viewBox size, PL/PB =
// left/bottom padding, mx = max value, X()/Y() = data to SVG coordinate.

// Stacked area chart, one band per family (e.g. cost per model over time).
//   series   - [{ d: "YYYY-MM-DD", [family]: number }]
//   families - ordered family names to stack
// `preserveAspectRatio=none` smears the glyphs at narrow widths, so `.axlab` is
// dropped below md and the per-point <title> tooltips carry the figures.
// A family name comes off the wire: `Object.hasOwn` keeps a day with no own
// property from reading `__proto__` off the prototype chain.
function stackedAreaChart(series, families, { h = 230 } = {}) {
  if (series.length < 2) return `<div class=empty>Not enough data points</div>`;
  const W = 1000,
    PL = 46,
    PB = 24;
  const tot = series.map((s) => families.reduce((a, f) => a + (Object.hasOwn(s, f) ? s[f] : 0), 0)),
    mx = Math.max(...tot, 0) || 1;
  const X = (i) => PL + (i * (W - PL - 8)) / (series.length - 1),
    Y = (v) => h - PB - (v / mx) * (h - PB - 10);
  let acc = series.map(() => 0),
    out = "";
  for (const f of families) {
    const lo = acc.slice();
    acc = acc.map((v, i) => v + (Object.hasOwn(series[i], f) ? series[i][f] : 0));
    // The bottom edge is walked backwards, so the outline closes instead of crossing.
    const top = acc.map((v, i) => `${i ? "L" : "M"}${X(i)},${Y(v)}`).join("");
    const bottom = lo
      .map((v, i) => `L${X(series.length - 1 - i)},${Y(lo[series.length - 1 - i])}`)
      .join("");
    // fill-opacity is a real CSS property: a light theme raises it in the stylesheet.
    out += `<path class=area d="${top}${bottom}Z"
      fill="${modelColor(f)}" stroke="${modelColor(f)}" stroke-width="1.4"/>`;
  }
  const grid = [0, 0.5, 1]
    .map(
      (g) => `<line x1=${PL} x2=${W} y1=${Y(mx * g)} y2=${Y(mx * g)}
    stroke=var(--grid) stroke-dasharray="3 4"/><text class=axlab x=${PL - 8} y=${Y(mx * g) + 4}
    fill=var(--dim2) font-size=11 text-anchor=end>$${(mx * g).toFixed(1)}</text>`,
    )
    .join("");
  const step = Math.max(1, Math.ceil(series.length / 9));
  const dots = series
    .map((s, i) => {
      const lines = families
        .map((f) => `\n${escapeHtml(f)} : $${(Object.hasOwn(s, f) ? s[f] : 0).toFixed(3)}`)
        .join("");
      return `<circle cx=${X(i)} cy=${Y(tot[i])} r=9 fill=transparent><title>${escapeHtml(s.d)}
    ${lines}</title></circle>`;
    })
    .join("");
  return `<svg viewBox="0 0 ${W} ${h}" preserveAspectRatio=none style="height:${h}px">
    ${grid}${out}${series
      .map((s, i) =>
        i % step
          ? ""
          : `<text class=axlab x=${X(i)} y=${h - 6}
    fill=var(--dim2) font-size=11 text-anchor=middle>${escapeHtml(s.d.slice(5))}</text>`,
      )
      .join("")}
    ${dots}</svg>`;
}

// Weekly rhythm from a [7][24] count grid: a weekday per column, an hour per
// row, so a day reads top to bottom the way a calendar does.
const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function rhythmGrid(rhythm) {
  const max = Math.max(...rhythm.flat(), 0);
  if (!max) return `<div class=empty>No activity</div>`;
  // Wider than tall: 24 square rows run off the fold, and a day name must fit.
  const CW = 46,
    CH = 15,
    PL = 30,
    PT = 16,
    H = PT + 24 * CH;
  const cells = rhythm
    .map((day, d) =>
      day
        .map(
          (n, h) => `<rect x=${d * CW + PL} y=${PT + h * CH} width=${CW - 3} height=${CH - 2} rx=2
      fill="${n ? "var(--acc-fill)" : "var(--well)"}" fill-opacity="${(n ? 0.2 + (0.8 * n) / max : 1).toFixed(2)}">
      <title>${escapeHtml(DAY_LABELS[d])} ${h}h : ${escapeHtml(formatNumber(n))}</title></rect>`,
        )
        .join(""),
    )
    .join("");
  const days = DAY_LABELS.map(
    (l, d) => `<text x=${d * CW + PL + (CW - 3) / 2} y=${PT - 5} fill=var(--dim2)
    font-size=10 text-anchor=middle>${l}</text>`,
  ).join("");
  // Every third hour: 24 labels at this row height would collide.
  const hours = rhythm[0]
    .map((_, h) =>
      h % 3
        ? ""
        : `<text x=${PL - 6} y=${PT + h * CH + 11} fill=var(--dim2)
      font-size=10 text-anchor=end>${h}h</text>`,
    )
    .join("");
  // Capped at its natural size, shrinking under it: a fixed height would fix the
  // intrinsic width too, and 352px overflows its box below a ~1500px viewport.
  const W = PL + 7 * CW;
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;max-width:${W}px;height:auto">
    ${days}${cells}${hours}</svg>`;
}

// Horizontal labelled bars over [{ label, value }]. The two tracks are utilities
// and not inline `flex`: an inline style is unreachable by a breakpoint, and at
// 118 + 74 the bar itself would get 81px of a 297px box.
function horizontalBars(items, { fmt = formatMoney } = {}) {
  const mx = Math.max(...items.map((i) => i.value), 1);
  return items
    .map(
      (i) => `<div style="display:flex;align-items:center;gap:12px;padding:5px 0">
    <span class="dim grow-0 shrink-0 basis-[118px] max-md:basis-16" style="text-align:right;font-size:12.5px;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${escapeHtml(i.label)}</span>
    <span style="flex:1;background:var(--well);border-radius:4px;height:19px">
      <span style="display:block;height:100%;width:${((100 * i.value) / mx).toFixed(2)}%;background:var(--acc-fill);
      border-radius:4px"></span></span>
    <span class="num grow-0 shrink-0 basis-[74px] max-md:basis-16">${escapeHtml(fmt(i.value))}</span></div>`,
    )
    .join("");
}

// Context in use across a session, with a dashed mark at each compaction. The
// marks come from the compaction timestamps, never from the curve: a drop is one
// step between two requests, and reading it off the points would displace it.
function contextSparkline(points, compactions = [], { h = 58 } = {}) {
  if (points.length < 2) return `<div class=empty>Not enough requests</div>`;
  const W = 300;
  const t0 = points[0].ts,
    t1 = points.at(-1).ts;
  const span = t1 - t0 || 1;
  // Headroom: a curve touching the top edge reads as a ceiling.
  const mx = (Math.max(...points.map((p) => p.value)) || 1) * 1.1;
  const X = (ts) => ((W * (ts - t0)) / span).toFixed(1),
    Y = (v) => (h - (v / mx) * h).toFixed(1);
  const coords = points.map((p) => `${X(p.ts)},${Y(p.value)}`);
  const marks = compactions
    .filter((c) => c.ts >= t0 && c.ts <= t1)
    .map(
      (c) => `<line x1="${X(c.ts)}" x2="${X(c.ts)}" y1="0" y2="${h}"
    stroke="var(--amber)" stroke-width="1" stroke-dasharray="2 2"/>`,
    )
    .join("");
  return `<svg viewBox="0 0 ${W} ${h}" preserveAspectRatio=none
    style="width:100%;height:${h}px;display:block">
    ${marks}<path d="M0,${h}L${coords.join("L")}L${W},${h}Z"
    fill="var(--acc-fill)" opacity="0.22"/>
    <polyline points="${coords.join(" ")}" fill="none"
    stroke="var(--acc)" stroke-width="1.5"/></svg>`;
}

// Each token type's share of the total, from a type-to-weighted-value map.
function weightBar(weights) {
  const total = Object.values(weights).reduce((sum, v) => sum + v, 0) || 1;
  const segments = [...TOKEN_TYPES].map(([type, { label, cls }]) => {
    const share = ((100 * (weights[type] || 0)) / total).toFixed(2);
    return `<i class="${cls}" style="width:${share}%" title="${label}: ${escapeHtml(formatNumber(weights[type]))}"></i>`;
  });
  return `<div class=stack>${segments.join("")}</div>`;
}

export { stackedAreaChart, rhythmGrid, horizontalBars, weightBar, contextSparkline };
