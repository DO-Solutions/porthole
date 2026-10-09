// Dashboards (/dashboards): the committed Kraken's Eye file, its variables and thresholds, and each chart's
// query run here (dashboards have no API, so this is as close as Porthole can get).
import { chartActions, drawRange } from "../charts.js";
import { $, api, bindRegionLink, clear, copy, el, errorText, frame, jsonTree, region } from "../porthole.js";

const cfg = await frame();
let handle = null;

function table(headers, rows) {
  return el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))), el("tbody", {}, rows));
}

async function runChart(index, chart) {
  const out = clear($("#run-output"));
  const box = el("div", { class: "chart loading" });
  const actions = el("div", { class: "card-actions" });
  out.append(el("div", { class: "detail" }, el("h3", {}, `${chart.title} (${chart.type}), ${region(cfg)}`), box, actions));
  try {
    const p = await api("/api/dashboards/krakens-eye/run", { params: { index, region: region(cfg), range: "1h" } });
    handle = drawRange(box, p, { empty: "Insights returned no series for this chart's query." });
    chartActions(actions, () => handle, { promql: () => p.promql, link: cfg.links["insights.dashboards"], copy });
  } catch (e) { clear(box).append(el("p", { class: "empty" }, errorText(e))); box.classList.remove("loading"); }
}

function renderSidecar(side) {
  const vars = clear($("#variables"));
  vars.append(el("h3", {}, "Variables"), table(["name", "type", "values from", "default", "multi"],
    (side.variables || []).map((v) => el("tr", {}, el("td", {}, `$${v.name}`), el("td", {}, v.type),
      el("td", {}, [v.from, v.regex && `regex ${v.regex}`].filter(Boolean).join(", ")), el("td", {}, String(v.default ?? "")),
      el("td", {}, v.multi ? "yes" : "no")))));
  const withThresholds = (side.charts || []).filter((c) => (c.thresholds || []).length);
  vars.append(el("h3", {}, "Thresholds"), table(["chart", "mode", "value", "color"],
    withThresholds.flatMap((c) => c.thresholds.map((t) => el("tr", {}, el("td", {}, c.title), el("td", {}, t.mode),
      el("td", { class: "num" }, String(t.value)), el("td", {}, t.color))))));
  const rows = (side.charts || []).map((c, i) => el("tr", {},
    el("td", {}, c.title), el("td", {}, c.type), el("td", {}, c.group || ""),
    el("td", {}, c.promql ? el("code", {}, c.promql) : el("span", { class: "dim" }, c.logs ? "logs query" : "no query")),
    el("td", {}, el("code", {}, c.legend || "")),
    el("td", {}, c.promql ? el("button", { type: "button", onclick: () => runChart(i, c) }, "Run in Porthole")
      : el("span", { class: "small dim" }, c.type))));
  clear($("#charts-table")).append(table(["chart", "type", "group", "PromQL", "legend", ""], rows));
}

const link = cfg.links["insights.dashboards"] || {};
bindRegionLink($("#dash-link"), link);
if (!link.verified) $("#dash-link").setAttribute("title", "link pattern not verified yet (see BUGS.md)");
try {
  const data = await api("/api/dashboards/krakens-eye");
  if (data.sidecar) renderSidecar(data.sidecar);
  else {
    const note = "The sidecar krakens-eye.queries.json is missing, so the charts cannot be listed. This is the raw dashboard file.";
    let raw = null;
    try { raw = JSON.parse(data.raw || "null"); } catch { raw = data.raw; }
    clear($("#charts-table")).append(el("p", { class: "banner attention" }, note), raw ? jsonTree(raw) : el("p", {}, "No file either."));
    clear($("#variables")).append(el("p", { class: "empty" }, "Listed from the sidecar, which is missing."));
  }
} catch (e) { clear($("#charts-table")).append(el("p", { class: "empty" }, errorText(e))); }
