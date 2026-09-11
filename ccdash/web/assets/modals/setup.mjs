import { escapeHtml } from "../format.mjs";
import { modalBox } from "../components.mjs";

// The global env block, in README.md's order; endpoint, the three flags and the
// host come from the form, the rest are fixed.
const envEntries = ({ endpoint, host, tools, prompts, responses }) => [
  ["CLAUDE_CODE_ENABLE_TELEMETRY", "1"],
  ["OTEL_METRICS_EXPORTER", "otlp"],
  ["OTEL_METRICS_INCLUDE_ACCOUNT_UUID", "false"],
  ["OTEL_EXPORTER_OTLP_PROTOCOL", "http/json"],
  ["OTEL_EXPORTER_OTLP_ENDPOINT", `http://${endpoint.trim()}:4318`],
  ["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", "delta"],
  ["OTEL_LOGS_EXPORTER", "otlp"],
  ["OTEL_LOG_TOOL_DETAILS", tools ? "1" : "0"],
  ["OTEL_LOG_USER_PROMPTS", prompts ? "1" : "0"],
  ["OTEL_LOG_ASSISTANT_RESPONSES", responses ? "1" : "0"],
  ["OTEL_RESOURCE_ATTRIBUTES", `host=${host.trim()}`],
];

// A blank project drops the segment rather than emitting `project=`.
const resourceAttributes = (host, project) =>
  project ? `host=${host},project=${project}` : `host=${host}`;

// The two settings.json blocks a reader pastes. JSON.stringify escapes the typed
// values, so both are safe to drop in via textContent.
const buildSettings = (opts) => {
  const toJson = (value) => JSON.stringify(value, null, 2);
  return {
    global: toJson({ env: Object.fromEntries(envEntries(opts)) }),
    project: toJson({
      env: {
        OTEL_RESOURCE_ATTRIBUTES: resourceAttributes(opts.host.trim(), opts.project.trim()),
      },
    }),
  };
};

const SETUP_INPUT =
  "display:block;width:100%;margin-top:4px;font:13px var(--fu);background:var(--card);" +
  "color:var(--tx);border:1px solid var(--line);border-radius:8px;padding:7px 11px";
const SETUP_FLAGS = [
  ["tools", "OTEL_LOG_TOOL_DETAILS", "Bash commands, sub-agent types, MCP names"],
  ["prompts", "OTEL_LOG_USER_PROMPTS", "session titles and prompt text, in clear"],
  ["responses", "OTEL_LOG_ASSISTANT_RESPONSES", "assistant answers, in clear"],
];

const setupCheckbox = ([key, label, note]) =>
  `<label style="display:flex;gap:8px;align-items:baseline;margin-top:6px">
    <input type=checkbox data-setup-${key} checked>
    <span><code>${label}</code> <span class=cap>&mdash; ${note}</span></span>
  </label>`;

// `data-setup-block` scopes the copy handler to its own <pre>.
const setupBlock = ({ name, title, path, json }) =>
  `<div data-setup-block>
    <div style="display:flex;justify-content:space-between;align-items:center;margin:16px 0 6px">
      <h3 style="font-size:13px;margin:0">${title} <code>${path}</code></h3>
      <button data-setup-copy>Copy</button>
    </div>
    <pre data-setup-snippet="${name}" class="snippet whitespace-pre max-h-[32vh] overflow-auto">${escapeHtml(
      json,
    )}</pre>
  </div>`;

// Read-only: the reader pastes the snippets, ccdash never writes them. The live
// update and copy handlers live in app.mjs, keyed on `data-setup-*`.
const SETUP_DEFAULT_ENDPOINT = "127.0.0.1";

const setupModal = (d) => {
  const host = d.server_host ?? "";
  const project = d.project ?? "";
  const settings = buildSettings({
    endpoint: SETUP_DEFAULT_ENDPOINT,
    host,
    project,
    tools: true,
    prompts: true,
    responses: true,
  });
  // `data-setup-form` scopes the live input handler in app.mjs to this box.
  return modalBox({
    attr: "data-setup-form",
    title: "Set up telemetry",
    cap: `Claude Code exports nothing until you add these <code>env</code> blocks and
    restart your session. Fill the fields; copy each block.`,
    body: `<div class="max-md:flex-col" style="display:flex;gap:12px;margin-top:8px">
    <label style="flex:1;min-width:0"><span class=cap>host</span>
      <input data-setup-host value="${escapeHtml(host)}" style="${SETUP_INPUT}">
      <span class=cap style="display:block;margin-top:4px">Pre-filled from the ccdash
        host &mdash; correct it if ccdash runs on another machine.</span>
    </label>
    <label style="flex:1;min-width:0"><span class=cap>project (optional)</span>
      <input data-setup-project value="${escapeHtml(project)}" style="${SETUP_INPUT}">
      <span class=cap style="display:block;margin-top:4px">Per-repository tag &mdash; better
        set in the repo's own <code>.claude/settings.json</code>.</span>
    </label>
    <label style="flex:1;min-width:0"><span class=cap>ccdash endpoint</span>
      <input data-setup-endpoint value="${SETUP_DEFAULT_ENDPOINT}" style="${SETUP_INPUT}">
      <span class=cap style="display:block;margin-top:4px">Host:port ccdash listens on
        &mdash; the OTLP port <code>4318</code> is appended.</span>
    </label>
  </div>
  <div style="margin-top:10px">${SETUP_FLAGS.map(setupCheckbox).join("")}</div>
  ${setupBlock({ name: "global", title: "Global", path: "~/.claude/settings.json", json: settings.global })}
  ${setupBlock({ name: "project", title: "Per-repository", path: ".claude/settings.json", json: settings.project })}
  <p class=cap style="margin-top:10px">Copy-paste only: ccdash never writes your settings.</p>
  `,
  });
};

export { setupModal, buildSettings };
