// Metrics (/metrics): the region's catalog, up to six builder charts, and raw PromQL for the captain.
import { chartActions, drawRange } from "../charts.js";
import {
  $, api, clear, copy, el, errorText, fmtTime, frame, isCaptain, needsKey, onCaptain, onRegion, region, regionsFor,
  swatch, toast,
} from "../porthole.js";

const cfg = await frame();
const MAX_CHARTS = 6;
const FAMILY_KIND = { "do.droplets": "tentacle", "do.apps": "app", "do.load_balancers": "load_balancer",
  "do.databases": "database", "do.kubernetes": "kubernetes", "do.functions": "functions", "do.spaces": "spaces",
  "do.container_registry": "registry" };
const cards = [];
let timer = null;

const rangeSel = $("#range");
for (const r of cfg.caps.ranges || ["30m"]) rangeSel.append(el("option", { value: r, selected: r === "30m" ? true : null }, r));

function familyOf(metric) { return metric.split(".").slice(0, 2).join("."); }

async function loadCatalog() {
  const box = clear($("#catalog"));
  box.append(el("p", { class: "empty" }, "loading the catalog..."));
  try {
    const data = await api("/api/insights/catalog", { params: { region: region(cfg) } });
    const per = data.regions || { [data.region]: data };
    const families = {};
    let count = 0;
    clear(box);
    for (const [r, d] of Object.entries(per)) {
      if (d.error) box.append(el("p", { class: "chart-notice" }, `${r}: ${d.error.message}`));
      if (d.stale) box.append(el("p", { class: "chart-notice" }, `${r}: cached list (call budget used up)`));
      count += d.count || 0;
      for (const [f, names] of Object.entries(d.families || {})) {
        families[f] ||= new Map();
        names.forEach((n) => families[f].set(n.dotted, n));
      }
    }
    for (const f of Object.keys(families).sort()) {
      const group = el("details", { open: f === "do.droplets" ? true : null }, el("summary", {}, `${f} (${families[f].size})`));
      for (const n of [...families[f].values()].sort((a, b) => a.dotted.localeCompare(b.dotted))) {
        group.append(el("button", { type: "button", title: `returned as ${n.underscored}`, onclick: () => addCard(n.dotted) },
          n.dotted.slice(f.length + 1)));
      }
      box.append(group);
    }
    if (!Object.keys(families).length) box.append(el("p", { class: "empty" }, "No metrics with data in this region in the last 30 minutes."));
    $("#catalog-count").textContent = `${count} names`;
  } catch (e) { clear(box).append(el("p", { class: "empty" }, errorText(e))); }
}

function chipsFor(card) {
  const kind = FAMILY_KIND[familyOf(card.metric)];
  const regions = regionsFor(region(cfg), cfg);
  const box = clear(card.chips);
  for (const e of cfg.entities.filter((x) => x.kind === kind && regions.includes(x.region))) {
    const chip = el("button", { type: "button", class: "chip", "aria-pressed": String(card.names.has(e.name)), onclick: () => {
      if (card.names.has(e.name)) card.names.delete(e.name); else card.names.add(e.name);
      chip.setAttribute("aria-pressed", String(card.names.has(e.name)));
      run(card);
    } }, swatch(e.slot), e.display);
    box.append(chip);
  }
}

async function run(card, asWritten = false) {
  card.chartBox.classList.add("loading");
  try {
    const p = asWritten
      ? await api("/api/insights/promql", { method: "POST", captain: true,
        body: { region: region(cfg), query: card.promql.value, range: rangeSel.value } })
      : await api("/api/insights/range", { params: { region: region(cfg), metric: card.metric, agg: card.agg,
        // the head turns each fleet name into its resource_urn (fresh Droplets have no resource_name, B-023)
        range: rangeSel.value, filters: [...card.names].map((n) => `resource_name=${n}`) } });
    if (!asWritten) card.promql.value = p.promql;
    card.handle = drawRange(card.chartBox, p);
    const calls = Object.values(p.regions || {}).reduce((n, r) => n + (r.calls || []).length, 0);
    card.status.textContent = `${p.cached ? "cached" : "fetched"} ${fmtTime(p.fetched_at)}, ${calls} call(s)`;
  } catch (e) {
    clear(card.chartBox).append(el("p", { class: "empty" }, errorText(e)));
  }
}

function addCard(metric, agg = "avg") {
  if (cards.length >= MAX_CHARTS) { toast("Six charts at most; remove one first.", "attention"); return; }
  if (cards.some((c) => c.metric === metric)) { toast(`${metric} is already on the page.`); return; }
  const card = { metric, agg, names: new Set(), handle: null };
  card.chartBox = el("div", { class: "chart" });
  card.promql = el("textarea", { class: "promql", rows: "2", readonly: true, "aria-label": `PromQL for ${metric}` });
  card.chips = el("div", { class: "chips", role: "group", "aria-label": "fleet member filter" });
  card.status = el("span", { class: "small dim" });
  const aggSel = el("select", { "aria-label": "aggregate", onchange: (ev) => { card.agg = ev.target.value; run(card); } },
    ["avg", "sum", "max", "min", "rate", "none"].map((a) => el("option", { value: a, selected: a === agg ? true : null }, a)));
  const asWritten = needsKey(el("button", { type: "button", onclick: () => run(card, true) }, "Run as written"));
  const actions = el("div", { class: "card-actions" });
  card.node = el("article", { class: "card" },
    el("div", { class: "card-head" }, el("h2", {}, metric), el("button", { type: "button", onclick: () => {
      cards.splice(cards.indexOf(card), 1); card.node.remove();
    } }, "remove")),
    card.promql,
    el("div", { class: "row" }, el("label", { class: "field" }, el("span", { class: "label" }, "aggregate"), aggSel),
      el("button", { type: "button", class: "primary", onclick: () => run(card) }, "Run"), asWritten, card.status),
    card.chips, card.chartBox, actions);
  chartActions(actions, () => card.handle, { promql: () => card.promql.value, link: cfg.links["insights.metrics"], copy });
  card.promql.readOnly = !isCaptain();
  $("#charts").append(card.node);
  cards.push(card);
  chipsFor(card);
  run(card);
}

function rerunAll() { cards.forEach((c) => run(c)); }

$("#refresh").addEventListener("change", (ev) => {
  clearInterval(timer);
  const s = Number(ev.target.value);
  timer = s ? setInterval(rerunAll, s * 1000) : null;
  $("#metrics-status").textContent = s ? `refreshing every ${s} s` : "";
});
rangeSel.addEventListener("change", rerunAll);
onRegion(() => { loadCatalog(); cards.forEach((c) => { c.names.clear(); chipsFor(c); }); rerunAll(); });
onCaptain((aboard) => cards.forEach((c) => { c.promql.readOnly = !aboard; }));

const wanted = new URL(location.href).searchParams.get("metric");
loadCatalog();
addCard(wanted && /^do(\.[a-z0-9_]+){2,}$/.test(wanted) ? wanted : "do.droplets.cpu_utilization");
