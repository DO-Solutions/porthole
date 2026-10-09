// Shared code for every page: the frame (nav, region, captain's key, API drawer), fetch, SSE, DOM helpers.
// Data is always inserted with textContent; nothing here builds HTML from strings.

const PAGES = [["/", "Bridge"], ["/stir", "Stir the water"], ["/metrics", "Metrics"], ["/dashboards", "Dashboards"],
  ["/alerts", "Alerts"], ["/logs", "Logs"], ["/traces", "Traces"], ["/api", "API"], ["/brain", "Brain"]];
const KEY_STORE = "porthole.captain";
const REGION_STORE = "porthole.region";
const EVENT_NAMES = ["api_call", "fleet", "scenario", "voyage", "delivery", "brain", "heartbeat"];

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "vars") for (const [p, val] of Object.entries(v)) node.style.setProperty(p, val);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); return node; }

export class ApiError extends Error {
  constructor(status, body, retryAfter) {
    const err = (body && body.error) || {};
    super(err.message || `HTTP ${status}`);
    this.status = status; this.code = err.code || "error"; this.detail = err.detail; this.retryAfter = retryAfter;
  }
}

export async function api(path, { method = "GET", body, captain = false, params } = {}) {
  let url = path;
  if (params) {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      (Array.isArray(v) ? v : [v]).forEach((x) => q.append(k, x));
    }
    const s = q.toString();
    if (s) url += (url.includes("?") ? "&" : "?") + s;
  }
  const headers = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (captain && captainKey()) headers["X-Captain-Key"] = captainKey();
  const resp = await fetch(url, { method, headers, credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body) });
  if (resp.status === 204) return null;
  let data = null;
  try { data = await resp.json(); } catch { data = null; }
  if (!resp.ok) throw new ApiError(resp.status, data, resp.headers.get("Retry-After"));
  return data;
}

let configPromise = null;
export function config() {
  if (!configPromise) configPromise = api("/api/config");
  return configPromise;
}

// --- region ------------------------------------------------------------------------------------
const regionListeners = [];
export function regionChoices(cfg) { return [...(cfg.regions || []), "both"]; }
export function region(cfg) {
  const fromUrl = new URL(location.href).searchParams.get("region");
  for (const r of [fromUrl, sessionStorage.getItem(REGION_STORE), cfg.region_default]) {
    if (r && regionChoices(cfg).includes(r)) return r;
  }
  return regionChoices(cfg)[0];
}
export function regionsFor(r, cfg) { return r === "both" ? cfg.regions : [r]; }
export function onRegion(fn) { regionListeners.push(fn); }
function setRegion(r) {
  sessionStorage.setItem(REGION_STORE, r);
  const u = new URL(location.href);
  u.searchParams.set("region", r);
  history.replaceState(null, "", u);
  document.querySelectorAll(".region-choice").forEach((b) => b.setAttribute("aria-pressed", String(b.value === r)));
  regionListeners.forEach((fn) => fn(r));
}

// --- captain's key -------------------------------------------------------------------------------
const captainListeners = [];
export function captainKey() { return sessionStorage.getItem(KEY_STORE) || ""; }
export function isCaptain() { return Boolean(captainKey()); }
export function onCaptain(fn) { captainListeners.push(fn); }
export function needsKey(node) { node.dataset.needsKey = "1"; applyCaptain(node); return node; }
function applyCaptain(only) {
  const nodes = only ? [only] : document.querySelectorAll("[data-needs-key]");
  nodes.forEach((n) => { n.disabled = !isCaptain(); n.title = isCaptain() ? "" : "needs the captain's key"; });
  const btn = $(".captain-btn");
  if (btn && !only) {
    btn.textContent = isCaptain() ? "captain aboard" : "captain's key: off";
    btn.classList.toggle("aboard", isCaptain());
  }
  if (!only) captainListeners.forEach((fn) => fn(isCaptain()));
}

function keyDialog() {
  const input = el("input", { type: "password", autocomplete: "off", placeholder: "paste the captain's key",
    "aria-label": "captain's key" });
  const msg = el("p", { class: "small dim" }, "The key stays in this tab (sessionStorage) and is sent as a header.");
  const dialog = el("dialog", { "aria-label": "captain's key" }, el("h2", { text: "Captain's key" }), msg, input);
  const leave = el("button", { type: "button", onclick: () => { sessionStorage.removeItem(KEY_STORE); applyCaptain();
    dialog.close(); } }, "Leave the bridge");
  const check = el("button", { type: "button", class: "primary", onclick: async () => {
    sessionStorage.setItem(KEY_STORE, input.value.trim());
    try {
      await api("/api/captain/check", { method: "POST", captain: true });
      applyCaptain(); input.value = ""; dialog.close(); toast("Captain aboard. Mutating controls are on.");
    } catch (e) {
      sessionStorage.removeItem(KEY_STORE); applyCaptain();
      msg.textContent = e.status === 401 ? "That key was not accepted." : e.status === 503
        ? "The captain's key is not configured on this server." : e.status === 429
          ? `Too many tries; wait ${e.retryAfter || 60} s.` : `Check failed: ${e.message}`;
    }
  } }, "Check key");
  dialog.append(el("div", { class: "row" }, check, leave,
    el("button", { type: "button", onclick: () => dialog.close() }, "Cancel")));
  input.addEventListener("keydown", (ev) => { if (ev.key === "Enter") check.click(); });
  document.body.append(dialog);
  return dialog;
}

// --- server-sent events ---------------------------------------------------------------------------
const handlers = {};
let source = null;
export function on(event, fn) {
  (handlers[event] ||= []).push(fn);
  if (!source) {
    source = new EventSource("/events");
    for (const name of EVENT_NAMES) {
      source.addEventListener(name, (ev) => {
        let data = null;
        try { data = JSON.parse(ev.data); } catch { return; }
        (handlers[name] || []).forEach((h) => h(data));
      });
    }
  }
}

// --- formatting ------------------------------------------------------------------------------------
export function fmtTime(iso) { return iso ? `${String(iso).slice(11, 19)}Z` : ""; }
export function fmtDateTime(iso) { return iso ? `${String(iso).slice(0, 10)} ${fmtTime(iso)}` : ""; }
export function fmtMs(ms) { return ms === null || ms === undefined ? "" : `${Math.round(ms)} ms`; }
export function fmtNum(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "";
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: digits });
}
export function fmtClock(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
export function shortPath(call) {
  const p = String(call.path || "").replace(/^\/v2\/insights\/query/, "...").replace(/^\/v2\/insights/, "...");
  return call.entity ? `${call.entity} ${p}` : p;
}

export function toast(message, kind = "good") {
  let box = $(".toasts");
  if (!box) { box = el("div", { class: "toasts", role: "status" }); document.body.append(box); }
  const t = el("div", { class: `toast ${kind}` }, message);
  box.append(t);
  setTimeout(() => t.remove(), 6000);
}

export function errorText(e) {
  if (e instanceof ApiError && e.code === "captain_key_required") return "needs the captain's key";
  return e && e.message ? e.message : String(e);
}

// --- fleet colors ----------------------------------------------------------------------------------
export function entity(cfg, name) { return (cfg.entities || []).find((e) => e.name === name || e.display === name); }
export function colorVar(slot) { return slot ? `var(--fleet-${slot})` : "var(--dim)"; }
export function swatch(slot) { return el("span", { class: "swatch", vars: { "--swatch": colorVar(slot) } }); }

// --- copy to clipboard -------------------------------------------------------------------------------
export async function copy(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); toast(`${what} to the clipboard.`); }
  catch { toast("The browser did not allow copying; select the text instead.", "attention"); }
}

// --- one API call in detail (drawer and API page) -----------------------------------------------------
export async function callDetail(container, id) {
  clear(container).append(el("p", { class: "dim" }, "loading..."));
  let c;
  try { c = await api(`/api/trace/${encodeURIComponent(id)}`); } catch (e) {
    clear(container).append(el("p", { class: "dim" }, errorText(e))); return;
  }
  const json = (v) => el("pre", { class: "block" }, v === null || v === undefined ? "none" : JSON.stringify(v, null, 2));
  const kv = el("dl", { class: "kv" });
  for (const [k, v] of [["time", c.t], ["target", c.target + (c.entity ? ` (${c.entity})` : "")], ["request",
    `${c.method} ${c.path}`], ["status", c.status ?? c.error], ["time taken", fmtMs(c.ms)], ["region", c.region || "-"],
  ["caller", c.caller || "-"], ["trace id", c.trace_id || "-"]]) kv.append(el("dt", {}, k), el("dd", {}, String(v ?? "")));
  clear(container).append(el("div", { class: "detail" }, kv,
    el("h3", {}, "parameters"), json(c.params), el("h3", {}, "request body (secrets replaced with ***)"), json(c.body),
    el("h3", {}, "response, first 2 KB"), el("pre", { class: "block" }, c.response_head || c.error || ""),
    el("div", { class: "card-actions" },
      el("button", { type: "button", onclick: () => copy(c.curl, "curl line copied") }, "Copy as curl"),
      el("button", { type: "button", onclick: () => copy(c.bugs_md, "BUGS.md entry copied") }, "Copy as BUGS.md entry"))));
}

// --- the frame ------------------------------------------------------------------------------------------
function topbar(cfg) {
  const nav = el("nav", { class: "nav", "aria-label": "pages" }, PAGES.map(([href, label]) =>
    el("a", { href, "aria-current": location.pathname === href ? "page" : null }, label)));
  const current = region(cfg);
  const regions = el("div", { class: "segmented", role: "group", "aria-label": "region" },
    regionChoices(cfg).map((r) => el("button", { type: "button", class: "region-choice", value: r,
      "aria-pressed": String(r === current), onclick: () => setRegion(r) }, r)));
  const dialog = keyDialog();
  const keyBtn = el("button", { type: "button", class: "captain-btn", onclick: () => dialog.showModal() });
  return el("div", { class: "topbar" },
    el("div", { class: "topbar-row" },
      el("a", { class: "brand", href: "/" }, el("img", { src: "/static/img/porthole.svg", alt: "" }),
        el("span", { class: "brand-name" }, "Porthole"),
        el("span", { class: "brand-tag" }, "a porthole onto DigitalOcean Insights")),
      el("div", { class: "frame-controls" }, el("span", { class: "label" }, "region"), regions, keyBtn)),
    nav);
}

function drawer() {
  const count = el("span", {}, "0 calls in the last minute");
  const last = el("span", { class: "last" }, "no Insights call yet");
  const rows = el("tbody");
  const detail = el("div");
  const body = el("div", { class: "drawer-body hidden" }, el("div", { class: "table-wrap" }, el("table", {},
    el("thead", {}, el("tr", {}, ["time", "target", "method", "path", "status", "ms"].map((h) => el("th", {}, h)))),
    rows)), detail);
  const bar = el("button", { type: "button", class: "drawer-bar", "aria-expanded": "false" },
    el("strong", {}, "API drawer"), count, last);
  bar.addEventListener("click", () => {
    body.classList.toggle("hidden");
    bar.setAttribute("aria-expanded", String(!body.classList.contains("hidden")));
  });
  const seen = [];
  const addRow = (c, top) => {
    const tr = el("tr", { class: "clickable", onclick: () => callDetail(detail, c.id) },
      el("td", { class: "nowrap" }, fmtTime(c.t)), el("td", {}, c.target), el("td", {}, c.method),
      el("td", {}, shortPath(c)), el("td", { class: "num" }, c.status ?? "err"), el("td", { class: "num" }, fmtMs(c.ms)));
    if (top) rows.prepend(tr); else rows.append(tr);
    while (rows.children.length > 50) rows.lastChild.remove();
  };
  const show = (c) => { last.textContent = `last: ${c.method} ${shortPath(c)} ${c.status ?? "error"} ${fmtMs(c.ms)}`; };
  const recount = () => {
    const now = Date.now();
    while (seen.length && now - seen[0] > 60000) seen.shift();
    count.textContent = `${seen.length} calls in the last minute`;
  };
  api("/api/trace", { params: { limit: 50 } }).then((data) => {
    data.calls.forEach((c) => addRow(c, false));
    for (let i = 0; i < data.stats.last_minute; i++) seen.push(Date.now());
    if (data.stats.last) show(data.stats.last);
    recount();
  }).catch(() => {});
  on("api_call", (c) => { addRow(c, true); show(c); seen.push(Date.now()); recount(); });
  setInterval(recount, 5000);
  return el("div", { class: "drawer" }, bar, body);
}

export async function frame() {
  let cfg;
  try { cfg = await config(); } catch (e) {
    cfg = { regions: ["tor1", "syd1"], region_default: "tor1", entities: [], fleet: { tentacles: [], sea: {}, rules: [] },
      features: {}, links: {}, caps: {}, problems: [`/api/config failed: ${errorText(e)}`] };
  }
  const top = $("#frame-top");
  if (top) clear(top).append(topbar(cfg));
  const bottom = $("#frame-drawer");
  if (bottom) clear(bottom).append(drawer());
  if ((cfg.problems || []).length) {
    const main = $("main");
    if (main) main.prepend(el("div", { class: "banner attention span-12" }, el("strong", {}, "Reduced mode. "),
      cfg.problems.join(" | ")));
  }
  applyCaptain();
  return cfg;
}
