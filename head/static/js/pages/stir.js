// Stir the water (/stir): the scenario console, what is running, the voyages and the live timeline.
import {
  $, api, clear, el, errorText, fmtClock, fmtTime, frame, needsKey, on, renderTimeline, toast,
} from "../porthole.js";

const cfg = await frame();
const catalog = (await api("/api/scenarios/catalog")).scenarios;
let scenario = catalog[0];
let target = (cfg.fleet.tentacles[0] || {}).name || "head";
let voyages = { voyages: [], runs: [], active_run_id: null };
let voyage = null;
let shownRun = null;
let listing = { running: [], finished: [] };

const display = (name) => (name === "head" ? "head" : ((cfg.entities || []).find((e) => e.name === name) || {}).display || name);

function renderTargets() {
  const box = clear($("#targets"));
  for (const t of [...cfg.fleet.tentacles.map((x) => x.name), "head"]) {
    const allowed = scenario.runs_on === "head" ? t === "head" : t !== "head";
    box.append(el("button", { type: "button", value: t, "aria-pressed": String(t === target), disabled: allowed ? null : true,
      onclick: () => { target = t; renderTargets(); } }, display(t)));
  }
}

function renderScenario() {
  if (scenario.runs_on === "head") target = "head";
  else if (target === "head") target = (cfg.fleet.tentacles[0] || {}).name;
  renderTargets();
  const tiles = clear($("#tiles"));
  for (const s of catalog) {
    tiles.append(el("button", { type: "button", class: "chip", "aria-pressed": String(s === scenario),
      onclick: () => { scenario = s; renderScenario(); } }, s.name));
  }
  $("#story").textContent = `${scenario.name}: ${scenario.story}`;
  const params = clear($("#params"));
  for (const p of scenario.params) {
    params.append(el("label", { class: "field" }, el("span", { class: "label" }, `${p.name} (${p.min} to ${p.max})`),
      el("input", { type: "number", name: p.name, min: p.min, max: p.max, step: p.type === "int" ? 1 : "any", value: p.default })));
  }
}

$("#start").addEventListener("click", async () => {
  const params = Object.fromEntries([...$("#params").querySelectorAll("input")].map((i) => [i.name, Number(i.value)]));
  try {
    const run = await api("/api/scenarios/start", { method: "POST", captain: true, body: { target, scenario: scenario.name, params } });
    const clamped = Object.entries(run.clamped || {}).map(([k, v]) => `${k} ${v.asked} to ${v.used}`);
    $("#start-note").textContent = `${run.id} on ${display(run.target)}${clamped.length ? `; clamped ${clamped.join(", ")}` : ""}`;
    loadListing();
  } catch (e) { toast(errorText(e), "failure"); }
});
needsKey($("#start"));

// how a finished run ended, and the status color for it
const REASONS = { finished: "completed", stopped: "stopped", failed: "error" };
const REASON_CLASS = { completed: "finished", stopped: "stopped", error: "failed" };

function limitOf(run) { return run.params && (run.params.seconds || null); }

function renderListing() {
  const running = clear($("#running"));
  if (!listing.running.length) running.append(el("p", { class: "empty" }, "Nothing is running."));
  for (const run of listing.running) {
    const started = Date.parse(run.started_at);
    const limit = limitOf(run);
    const clock = el("span", { class: "mono small", "data-started": String(started), "data-limit": String(limit || "") });
    running.append(el("div", { class: "row spread" }, el("span", {}, el("strong", {}, run.id), ` ${display(run.target)} `, clock),
      needsKey(el("button", { type: "button", onclick: async () => {
        try { await api("/api/scenarios/stop", { method: "POST", captain: true, body: { target: run.target, run_id: run.id } }); loadListing(); }
        catch (e) { toast(errorText(e), "failure"); }
      } }, "stop"))));
  }
  tick();
  const rows = listing.finished.slice(0, 50).map((r) => {
    const reason = r.reason || REASONS[r.status] || r.status;  // a tentacle not yet redeployed sends no reason
    const took = Number.isFinite(r.elapsed_s) ? ` (${fmtClock(r.elapsed_s)})` : "";
    return el("tr", {}, el("td", { class: "nowrap" }, fmtTime(r.started_at)),
      el("td", { class: "nowrap" }, `${fmtTime(r.ended_at)}${took}`),
      el("td", {}, display(r.target)), el("td", {}, r.name), el("td", { class: "small mono" }, JSON.stringify(r.params)),
      el("td", {}, el("span", { class: `status ${REASON_CLASS[reason] || r.status}` }, reason)),
      el("td", { class: "small dim" }, r.error || JSON.stringify(r.result || {})));
  });
  clear($("#finished")).append(rows.length ? el("table", {}, el("thead", {}, el("tr", {}, ["started", "ended", "target",
    "scenario", "params", "ended by", "result"].map((h) => el("th", {}, h)))), el("tbody", {}, rows))
    : el("p", { class: "empty" }, "None yet."));
}


function tick() {
  document.querySelectorAll("#running [data-started]").forEach((n) => {
    const elapsed = (Date.now() - Number(n.dataset.started)) / 1000;
    n.textContent = `${fmtClock(elapsed)}${n.dataset.limit ? ` / ${n.dataset.limit} s` : ""}`;
  });
}

async function loadListing() {
  try { listing = await api("/api/fleet/scenarios"); renderListing(); }
  catch (e) { clear($("#running")).append(el("p", { class: "empty" }, errorText(e))); }
}

function renderVoyages() {
  const tiles = clear($("#voyage-tiles"));
  for (const v of voyages.voyages) {
    tiles.append(el("button", { type: "button", class: "chip", "aria-pressed": String(voyage && v.name === voyage.name),
      onclick: () => { voyage = v; renderVoyages(); } }, v.title));
  }
  const controls = clear($("#voyage-controls"));
  if (!voyage) { $("#voyage-story").textContent = "Pick a voyage to see its plan."; return; }
  $("#voyage-story").textContent = `${voyage.title} (${voyage.feature}): ${voyage.story}`;
  let pick = null;
  if (voyage.params.target) {
    pick = el("select", { "aria-label": "target" }, el("option", { value: "" }, "default target"),
      cfg.fleet.tentacles.map((t) => el("option", { value: t.name }, t.display)));
    controls.append(el("label", { class: "field" }, el("span", { class: "label" }, "target"), pick));
  }
  const sailing = voyages.active_run_id;
  const sail = needsKey(el("button", { type: "button", class: "primary", onclick: async () => {
    try {
      const res = await api("/api/voyages/start", { method: "POST", captain: true,
        body: { voyage: voyage.name, params: pick && pick.value ? { target: pick.value } : {} } });
      shownRun = res.run_id; await loadVoyages();
    } catch (e) { toast(errorText(e), "failure"); }
  } }, "Set sail"));
  if (sailing) sail.disabled = true;
  controls.append(sail);
  if (sailing) {
    controls.append(el("span", { class: "small dim" }, `voyage ${sailing} is sailing; one at a time`),
      needsKey(el("button", { type: "button", class: "danger", onclick: async () => {
        try { await api(`/api/voyages/${encodeURIComponent(sailing)}/abort`, { method: "POST", captain: true }); loadVoyages(); }
        catch (e) { toast(errorText(e), "failure"); }
      } }, "Abort")));
  }
  controls.append(el("ol", { class: "small dim" }, voyage.steps.map((s) => el("li", {}, `${s.name}: ${s.title}`
    + (s.optional ? " (optional)" : "")))));
}

async function loadTimeline() {
  const id = shownRun || voyages.active_run_id || (voyages.runs[0] || {}).id;
  if (!id) return;
  try { renderTimeline($("#timeline"), await api(`/api/voyages/${encodeURIComponent(id)}`)); }
  catch (e) { clear($("#timeline")).append(el("p", { class: "empty" }, errorText(e))); }
}

async function loadVoyages() {
  try {
    voyages = await api("/api/voyages");
    if (!voyage) voyage = voyages.voyages.find((v) => v.name === new URL(location.href).searchParams.get("voyage")) || voyages.voyages[0];
    renderVoyages(); loadTimeline();
  } catch (e) { clear($("#voyage-tiles")).append(el("p", { class: "empty" }, errorText(e))); }
}

let pending = null;
on("scenario", () => loadListing());
on("voyage", (ev) => {
  if (!ev.step) loadVoyages();
  else { clearTimeout(pending); pending = setTimeout(loadTimeline, 300); }
});
renderScenario();
loadListing();
loadVoyages();
setInterval(tick, 1000);
setInterval(loadListing, 15000);
