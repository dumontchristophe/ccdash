const qs = (s) => document.querySelector(s);

// Compact number: 1234 -> "1.2K", 3.4e6 -> "3.4M", 2e9 -> "2.00G".
const formatNumber = (n) => {
  n = +n || 0;
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(2) + "G";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  // String whatever the magnitude, so a caller gets one behaviour.
  return String(Math.round(n));
};
// Human byte size: 1536 -> "1.5 KB".
const formatBytes = (n) => {
  n = +n || 0;
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (Math.abs(n) >= 1024 && i < 3) {
    n /= 1024;
    i++;
  }
  return (i ? n.toFixed(1) : Math.round(n)) + " " + u[i];
};
// What a byte size is worth once it is re-read: the telemetry carries bytes only,
// and four per token is a rule of thumb, off by a fifth on code. The one formatter
// returning markup, safe by construction but never usable inside an attribute.
const estTokens = (n) =>
  `<span title="Estimated from the result size, 4 bytes per token">~${formatNumber(
    Math.round((+n || 0) / 4),
  )}</span>`;
// Dollar amount with cents: 1.5 -> "$1.50".
const formatMoney = (n) => "$" + (+n || 0).toFixed(2);

// Duration in seconds -> "45s" / "1m 30s" / "2h 05min".
const formatDuration = (s) => {
  s = Math.round(+s || 0);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m " + String(s % 60).padStart(2, "0") + "s";
  // "min" and not "m": "2h 05" reads as a clock time.
  return Math.floor(s / 3600) + "h " + String(Math.floor((s % 3600) / 60)).padStart(2, "0") + "min";
};
// Unix seconds -> localized "Jan 12, 02:30 PM".
const formatDateTime = (t) =>
  new Date(t * 1000).toLocaleString("en-US", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
// Unix seconds -> localized "Wed, Jan 12".
const formatDate = (t) =>
  new Date(t * 1000).toLocaleDateString("en-US", {
    weekday: "short",
    day: "2-digit",
    month: "short",
  });
// Unix seconds -> localized "02:30:05 PM".
const formatTime = (t) =>
  new Date(t * 1000).toLocaleTimeString("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
// Escapes for element text and for an attribute in either kind of quote. The
// apostrophe is in the class so no template can break out of title='${...}'.
const escapeHtml = (s) =>
  String(s ?? "").replaceAll(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );
// Model family -> CSS color variable. A Map, not an object literal: the family is
// a payload value, and `short_model` capitalises past "constructor" but not
// "__proto__", which on a literal prints as [object Object] in a `fill`.
const MODEL_COLORS = new Map([
  ["Opus", "var(--opus)"],
  ["Sonnet", "var(--sonnet)"],
  ["Haiku", "var(--haiku)"],
  ["Fable", "var(--fable)"],
]);
const modelColor = (m) => MODEL_COLORS.get(m) || "var(--other)";

// The four token types, in the order they are stacked and legended. The keys are
// the snake_case names every payload carries; the OTLP spelling stops at the backend.
const TOKEN_TYPES = new Map([
  ["input", { label: "Input", cls: "s-input" }],
  ["cache_read", { label: "Cache read", cls: "s-cache-read" }],
  ["cache_creation", { label: "Cache write", cls: "s-cache-creation" }],
  ["output", { label: "Output", cls: "s-output" }],
]);

export {
  qs,
  formatNumber,
  formatBytes,
  estTokens,
  formatMoney,
  formatDuration,
  formatDateTime,
  formatDate,
  formatTime,
  escapeHtml,
  modelColor,
  TOKEN_TYPES,
};
