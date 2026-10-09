// The Bridge (/): the fleet with its Insights dots, the head's own request rate, and the water chart.
import { chartActions, drawRange, formatValue } from "../charts.js";
import { $, api, clear, copy, el, errorText, fmtTime, frame, on, onRegion, region } from "../porthole.js";

const cfg = await frame();
let water = null;

function dot(seen, checkedAt, reason) {
  const title = seen ? `Insights returned a sample in the last 5 minutes (checked ${fmtTime(checkedAt)})`
    : seen === false ? `no data in Insights right now (checked ${fmtTime(checkedAt)})` : reason || "not checked";
  return el("span", { class: `dot${seen ? " seen" : ""}${seen === null || seen === undefined ? " unknown" : ""}`,
    title, role: "img", "aria-label": seen ? "seen in Insights" : "not seen in Insights" });
}

function renderFleet(snap) {
  const list = clear($("#fleet-tentacles"));
  for (const t of snap.tentacles || []) {
    const h = t.health || {};
    const room = Number.isFinite(h.mem_avail_mb) ? `  room ${h.mem_avail_mb} MB` : "";
    const stats = t.reachable ? `load ${formatValue(h.load1, "plain")}  mem ${formatValue(h.mem_pct, "percent")}${room}`
      : `unreachable: ${t.error || ""}`;
    const running = (t.running || []).length;
    list.append(el("li", {}, dot(t.seen_in_insights, t.seen_checked_at, t.seen_reason),
      t.link ? el("a", { href: t.link, target: "_blank", rel: "noopener" }, t.display) : el("span", {}, t.display),
      el("span", { class: "small dim" }, `${t.region}  ${stats}`),
      el("span", { class: "small" }, running ? `${running} running` : "")));
  }
  if (!(snap.tentacles || []).length) list.append(el("li", { class: "empty" }, "No tentacles in the fleet description."));
  const sea = clear($("#fleet-sea"));
  const members = [["head", snap.head], ...Object.entries(snap.sea || {})].filter(([, v]) => v);
  for (const [kind, m] of members) {
    const name = kind === "head" ? "head" : kind.replace("_", " ");
    sea.append(el("span", {}, dot(m.seen_in_insights, snap.checked_at, m.seen_reason), " ",
      m.link ? el("a", { href: m.link, target: "_blank", rel: "noopener",
        title: m.link_verified ? m.name : `${m.name} (link pattern not verified)` }, name) : name));
  }
  $("#fleet-checked").textContent = snap.checked_at ? `checked ${fmtTime(snap.checked_at)}` : "";
}

async function loadSelf() {
  const head = cfg.fleet.head;
  const box = $("#self-chart");
  if (!head) { box.append(el("p", { class: "empty" }, "No head in the fleet description yet.")); return; }
  $("#self-link").setAttribute("href", head.link || "#");
  $("#self-metric").textContent = "do.apps.app_requests_per_second, last 30 min";
  box.classList.add("loading");
  try {
    const p = await api("/api/insights/range", { params: { region: head.region, metric: "do.apps.app_requests_per_second",
      filters: `resource_urn=${head.urn}`, range: "30m" } });
    drawRange(box, p, { spark: true, empty: "Insights has no request rate for this app yet." });
    const last = p.series.flatMap((s) => s.points).sort((a, b) => a[0] - b[0]).pop();
    $("#self-value").textContent = last ? formatValue(last[1], "plain") : "-";
  } catch (e) { clear(box).append(el("p", { class: "empty" }, errorText(e))); }
}

async function loadWater() {
  const box = $("#water-chart");
  box.classList.add("loading");
  const r = region(cfg);
  $("#water-title").textContent = `The water, last 30 min, ${r === "both" ? cfg.regions.join(" + ") : r}`;
  try {
    const p = await api("/api/insights/range", { params: { region: r, metric: "do.droplets.cpu_utilization",
      agg: "avg", range: "30m" } });
    water = drawRange(box, p, { empty: "Insights has no Droplet CPU for the fleet in this region yet." });
    $("#water-promql").textContent = "avg by (resource_urn) (do.droplets.cpu_utilization)";
  } catch (e) { clear(box).append(el("p", { class: "empty" }, errorText(e))); }
}

chartActions($("#water-actions"), () => water, { promql: () => water && water.payload.promql,
  link: cfg.links["insights.metrics"], copy });
try { renderFleet(await api("/api/fleet")); } catch (e) {
  clear($("#fleet-tentacles")).append(el("li", { class: "empty" }, errorText(e)));
}
on("fleet", renderFleet);
onRegion(loadWater);
loadSelf();
loadWater();
setInterval(() => { loadSelf(); loadWater(); }, 60000);
