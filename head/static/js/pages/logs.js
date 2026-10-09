// Logs (/logs): what Insights returns, what the head emitted, and what the tentacles say they wrote,
// with a banner that names the gap (finding A6b) when Insights has nothing for a log storm.
import {
  $, api, clear, el, errorText, fmtDateTime, fmtNum, frame, isCaptain, jsonTree, needsKey, on, onRegion, region,
} from "../porthole.js";

const cfg = await frame();
const TABS = [["insights", "Insights (API)"], ["head", "Head (as emitted)"], ["expected", "Expected from tentacles"]];
let page = null;
let expected = [];
let records = [];

for (const r of cfg.caps.ranges || ["1h"]) $("#range").append(el("option", { value: r, selected: r === "1h" ? true : null }, r));
const services = [...(cfg.fleet.tentacles || []).map((t) => t.service_name), cfg.fleet.head && cfg.fleet.head.service_name]
  .filter(Boolean);
services.forEach((s) => $("#service").append(el("option", { value: s }, s)));
needsKey($("#text"));

function logRegion() {
  const r = region(cfg);
  return r === "both" ? cfg.regions[0] : r;
}

function table(headers, rows, empty) {
  if (!rows.length) return el("p", { class: "empty" }, empty);
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))),
    el("tbody", {}, rows)));
}

function banner() {
  const box = $("#banner");
  const gap = expected.find((r) => r.verdict === "not collected (A6b)");
  box.className = `banner ${gap ? "failure" : ""}`;
  const note = region(cfg) === "both" ? ` Logs are one region at a time (finding A11); showing ${logRegion()}.` : "";
  box.textContent = gap ? gap.text : page ? `${page.summary} in ${page.region}.${note}` : `loading...${note}`;
}

function renderInsights() {
  const rows = records.map((r) => el("tr", {}, el("td", { class: "nowrap" }, fmtDateTime(r.timestamp)),
    el("td", {}, r.severity_text || String(r.severity_number ?? "")), el("td", {}, r.service_name || ""),
    el("td", {}, r.body || ""), el("td", { class: "mono small" }, (r.trace_id || "").slice(0, 12))));
  const box = clear($("#tab-insights"));
  box.append(table(["time (UTC)", "severity", "service", "body", "trace id"], rows, "Insights returned no records for this filter."));
  if (page && page.pagination && page.pagination.has_more && page.pagination.next_cursor) {
    box.append(el("button", { type: "button", onclick: () => loadInsights(page.pagination.next_cursor) }, "more"));
  }
  if (page) box.append(el("details", {}, el("summary", { class: "small dim" }, "request body sent to Insights"), jsonTree(page.body_sent)));
}

async function loadInsights(cursor = null) {
  const text = $("#text").value.trim();
  const service = $("#service").value;
  const severity = $("#severity").value;
  try {
    if (text && isCaptain()) {
      const parts = [{ text_search: { query: text } }];
      if (service) parts.push({ condition: { field: { name: "service.name" }, operator: "FILTER_OPERATOR_EQ", value: { string_value: service } } });
      if (severity) parts.push({ condition: { field: { name: "severity_number" }, operator: "FILTER_OPERATOR_GTE",
        value: { number_value: { DEBUG: 5, INFO: 9, WARN: 13, ERROR: 17 }[severity] } } });
      page = await api("/api/insights/logs/search", { method: "POST", captain: true,
        body: { region: logRegion(), range: $("#range").value, filter: parts.length > 1 ? { and: { expressions: parts } } : parts[0], limit: 100 } });
      records = page.records;
    } else {
      const params = { region: logRegion(), range: $("#range").value, service, severity, limit: 100 };
      if (cursor) Object.assign(params, { cursor, start: page.window.start, end: page.window.end });
      const next = await api("/api/insights/logs", { params });
      records = cursor ? records.concat(next.records) : next.records;
      page = next;
    }
  } catch (e) { page = null; records = []; clear($("#tab-insights")).append(el("p", { class: "empty" }, errorText(e))); banner(); return; }
  renderInsights(); banner();
}

async function loadHead() {
  const box = clear($("#tab-head"));
  try {
    const own = await api("/api/logs/own", { params: { limit: 300 } });
    box.append(el("p", { class: "small dim" }, `The last ${own.records.length} records this app wrote to stdout, which App Platform `
      + `forwards. Compare with what Insights returns for service.name = ${own.service_name}.`));
    box.append(table(["time (UTC)", "severity", "body", "trace id"], own.records.map((r) => el("tr", {},
      el("td", { class: "nowrap" }, fmtDateTime(r.timestamp)), el("td", {}, r.severity_text), el("td", {}, r.body),
      el("td", { class: "mono small" }, (r.trace_id || "").slice(0, 12)))), "No records yet."));
  } catch (e) { box.append(el("p", { class: "empty" }, errorText(e))); }
}

async function loadExpected() {
  const box = clear($("#tab-expected"));
  try {
    expected = await api("/api/insights/logs/expected", { params: { range: $("#range").value } });
    box.append(table(["tentacle", "run", "window (UTC)", "emitted", "Insights returned", "verdict"], expected.map((r) => el("tr", {},
      el("td", {}, `${r.display} (${r.region})`), el("td", { class: "mono small" }, r.run_id),
      el("td", { class: "nowrap" }, `${fmtDateTime(r.started_at)} to ${r.ended_at ? fmtDateTime(r.ended_at).slice(11) : "now"}`),
      el("td", { class: "num" }, `${fmtNum(r.emitted, 0)}${r.estimated ? " (estimate)" : ""}`,
        r.by_severity ? el("div", { class: "small dim" }, Object.entries(r.by_severity).map(([k, v]) => `${k} ${v}`).join(", ")) : null),
      el("td", { class: "num" }, r.insights_count === null ? "?" : `${fmtNum(r.insights_count, 0)}${r.insights_count_capped ? "+" : ""}`),
      el("td", {}, el("span", { class: `status ${r.verdict === "collected" ? "good" : r.verdict === "unknown" ? "attention" : "failure"}` },
        r.verdict), el("div", { class: "small" }, r.text)))),
    "No log storm in this window. Start a logs scenario or the Log storm voyage on the Stir page."));
  } catch (e) { expected = []; box.append(el("p", { class: "empty" }, errorText(e))); }
  banner();
}

const tabs = $("#tabs");
for (const [key, label] of TABS) {
  tabs.append(el("button", { type: "button", role: "tab", "aria-selected": String(key === "insights"), onclick: (ev) => {
    tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-selected", String(b === ev.target)));
    for (const [k] of TABS) $(`#tab-${k}`).classList.toggle("hidden", k !== key);
    if (key === "head") loadHead();
  } }, label));
}
function reload() { loadInsights(); loadExpected(); }
$("#search").addEventListener("click", reload);
$("#range").addEventListener("change", reload);
onRegion(reload);
on("scenario", (ev) => { if (ev.run && ev.run.name === "logs") loadExpected(); });
reload(); loadHead();
setInterval(loadExpected, 30000);
