// Traces (/traces): chain runs with their trace ids, the head's own spans from memory, and where they went.
import { $, api, clear, copy, el, errorText, fmtDateTime, fmtNum, frame } from "../porthole.js";

const cfg = await frame();
const link = cfg.links["insights.traces"] || {};
$("#traces-link").setAttribute("href", link.url || "#");
if (!link.verified) $("#traces-link").setAttribute("title", "link pattern not verified yet (see BUGS.md)");

function idCell(id) {
  if (!id) return el("td", {}, "");
  return el("td", { class: "mono small" }, `${id.slice(0, 16)}... `, el("button", { type: "button", class: "link",
    onclick: () => copy(id, "trace id copied") }, "copy"));
}

async function loadChains() {
  const box = clear($("#chains"));
  try {
    const data = await api("/api/traces/chains", { params: { range: "24h" } });
    if (!data.chains.length) { box.append(el("p", { class: "empty" }, "No chain runs in the last 24 hours. Start the Chain voyage or a chain scenario.")); return; }
    box.append(el("table", {}, el("thead", {}, el("tr", {}, ["tentacle", "run", "started (UTC)", "count", "ok", "failed",
      "first trace id", "last trace id"].map((h) => el("th", {}, h)))),
    el("tbody", {}, data.chains.map((c) => el("tr", {}, el("td", {}, c.display), el("td", { class: "mono small" }, c.run_id),
      el("td", { class: "nowrap" }, fmtDateTime(c.started_at)), el("td", { class: "num" }, String(c.count ?? "")),
      el("td", { class: "num" }, String(c.ok ?? "")), el("td", { class: "num" }, String(c.failed ?? "")),
      idCell(c.first_trace_id), idCell(c.last_trace_id))))));
  } catch (e) { box.append(el("p", { class: "empty" }, errorText(e))); }
}

function bars(trace) {
  const total = Math.max(trace.duration_ms, 0.001);
  return el("div", { class: "span-bars" }, trace.spans.slice(0, 40).map((s) => {
    const row = el("div", { class: `span-bar${s.status === "ERROR" ? " error" : ""}`,
      title: `${s.name}: ${fmtNum(s.duration_ms, 2)} ms, ${s.status}` },
    el("span", { class: "bar" }), el("span", { class: "name" }, `${s.name}  ${fmtNum(s.duration_ms, 1)} ms`));
    row.querySelector(".bar").style.setProperty("--left", `${(100 * s.offset_ms) / total}%`);
    row.querySelector(".bar").style.setProperty("--width", `${Math.max(0.5, (100 * s.duration_ms) / total)}%`);
    return row;
  }));
}

async function loadOwn() {
  const box = clear($("#own"));
  try {
    const data = await api("/api/traces/own", { params: { limit: 50 } });
    $("#export").textContent = data.export.text;
    if (!data.traces.length) { box.append(el("p", { class: "empty" }, "No spans yet.")); return; }
    for (const t of data.traces) {
      box.append(el("details", {}, el("summary", {}, el("strong", {}, t.root), ` ${fmtDateTime(t.started_at)}, `,
        `${fmtNum(t.duration_ms, 1)} ms, ${t.span_count} span(s) `, el("span", { class: `status ${t.status === "ERROR" ? "failure" : "good"}` },
          t.status.toLowerCase())),
      el("p", { class: "small mono" }, `trace ${t.trace_id} `, el("button", { type: "button", class: "link",
        onclick: () => copy(t.trace_id, "trace id copied") }, "copy")), bars(t)));
    }
  } catch (e) { box.append(el("p", { class: "empty" }, errorText(e))); }
}

loadChains(); loadOwn();
setInterval(() => { loadChains(); loadOwn(); }, 15000);
