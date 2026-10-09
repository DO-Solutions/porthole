// Alerts (/alerts): rules by id, instances, channels, webhook deliveries with their signature verdict,
// and the latest Alert round trip voyage.
import {
  $, api, clear, el, errorText, fmtDateTime, fmtTime, frame, jsonTree, needsKey, on, renderTimeline, toast,
} from "../porthole.js";

const cfg = await frame();
let data = null;
let filter = "all";

function table(headers, rows, empty) {
  if (!rows.length) return el("p", { class: "empty" }, empty);
  return el("table", {}, el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h)))), el("tbody", {}, rows));
}

function renderRules() {
  const names = Object.fromEntries((data.channels || []).map((c) => [c.id, c.name]));
  const rows = data.rules.map((r) => {
    const toggle = cfg.features.write && r.status !== "unknown" ? needsKey(el("button", { type: "button", onclick: async () => {
      try {
        await api(`/api/insights/rules/${encodeURIComponent(r.id)}/status`, { method: "POST", captain: true,
          body: { status: r.status === "active" ? "paused" : "active" } });
        toast(`${r.name}: ${r.status === "active" ? "paused" : "resumed"}`); load();
      } catch (e) { toast(errorText(e), "failure"); }
    } }, r.status === "active" ? "Pause" : "Resume")) : null;
    const cond = r.error ? r.error.message : `${r.operator} warning ${r.warning ?? "-"}, critical ${r.critical ?? "-"}`;
    return el("tr", {}, el("td", {}, r.name), el("td", {}, el("code", {}, r.metric || "")), el("td", {}, cond),
      el("td", {}, r.window || ""), el("td", {}, r.re_alert || ""),
      el("td", {}, el("span", { class: `status ${r.status === "active" ? "good" : r.error ? "failure" : "skipped"}` }, r.status)),
      el("td", {}, (r.channels || []).map((c) => `${names[c.id] || c.id}${c.notify_on.length ? ` (${c.notify_on.join(", ")})` : ""}`).join("; ")),
      el("td", {}, toggle));
  });
  // rules the team's instances name and the fleet description does not (finding A39), read-only
  for (const r of data.unknown_rules || []) {
    const cond = r.operator ? `${r.operator} warning ${r.warning ?? "-"}, critical ${r.critical ?? "-"}` : "";
    rows.push(el("tr", {}, el("td", {}, r.name || r.id, " ", label(r.label)), el("td", {}, el("code", {}, r.metric || "")),
      el("td", {}, cond), el("td", {}, r.window || ""), el("td", {}, r.re_alert || ""),
      el("td", {}, el("span", { class: `status ${r.status === "active" ? "good" : "skipped"}` }, r.status)),
      el("td", {}, (r.channels || []).map((c) => names[c.id] || c.id).join("; ")), el("td", {})));
  }
  clear($("#rules")).append(table(["name", "metric", "condition", "window", "re-alert", "status", "channels", ""], rows,
    "No rules in the fleet description."));
}

function label(text) { return text ? el("span", { class: "badge", title: "this rule is not in the fleet description" }, text) : null; }

function renderInstances() {
  const items = data.instances.filter((i) => filter === "all" || i.status === filter);
  const rows = items.map((i) => el("tr", {}, el("td", { class: "nowrap" }, fmtDateTime(i.triggered_at)),
    el("td", { class: "nowrap" }, i.resolved_at ? fmtDateTime(i.resolved_at) : ""),
    el("td", {}, i.rule_name || i.rule_id, " ", label(i.rule_label)),
    el("td", {}, i.entity || ""), el("td", {}, i.severity), el("td", { class: "num" }, String(i.value ?? "")),
    el("td", {}, el("span", { class: `status ${i.status === "active" ? "failure" : "good"}` }, i.status))));
  clear($("#instances")).append(table(["triggered", "resolved", "rule", "resource", "severity", "value", "status"], rows,
    filter === "active" ? "Nothing is alerting right now." : "No instances in the last 30 days."));
}

function renderChannels() {
  const box = clear($("#channels"));
  if (!data.channels.length) { box.append(el("p", { class: "empty" }, "No channels returned.")); return; }
  for (const c of data.channels) {
    const statuses = Object.entries(c.statuses || {}).map(([k, v]) => `${k}: ${JSON.stringify(v)}`);
    box.append(el("div", { class: "detail" }, el("strong", {}, c.name), ` ${c.type} `,
      c.points_here ? el("span", { class: "badge" }, "points at this site") : null,
      el("div", { class: "small mono" }, c.target), statuses.map((s) => el("div", { class: "small dim mono" }, s)),
      el("div", { class: "small dim" }, `used by ${c.rule_count ?? "?"} rule(s)`)));
  }
}

async function load() {
  try {
    data = await api("/api/insights/alerts");
    $("#alerts-status").textContent = `fetched ${fmtTime(data.fetched_at)}${data.stale ? " (cached: call budget used up)" : ""}`
      + (data.errors.length ? `; ${data.errors.length} error(s): ${data.errors.map((e) => e.message).join("; ")}` : "");
    renderRules(); renderInstances(); renderChannels();
  } catch (e) {
    for (const id of ["#rules", "#instances", "#channels"]) clear($(id)).append(el("p", { class: "empty" }, errorText(e)));
  }
}

function verdict(sig) {
  if (!sig) return "";
  if (sig.verified) return `verified: ${sig.matched.scheme} in ${sig.matched.header}`;
  return sig.note || (sig.headers_seen.length ? `not verified (${sig.tried} tries on ${sig.headers_seen.join(", ")})` : "no signature header");
}

async function showDelivery(id) {
  const box = clear($("#delivery-detail"));
  try {
    const d = await api(`/api/hooks/deliveries/${encodeURIComponent(id)}`);
    const when = el("h3", {}, `${d.id} at ${fmtDateTime(d.received_at)}`);
    if (!d.auth.ok) {  // a rejected delivery comes back as metadata only
      box.append(el("div", { class: "detail" }, when,
        el("p", {}, `auth: ${d.auth.configured}, rejected (scheme seen: ${d.auth.scheme_seen || "none"}); ${d.size} bytes, `
          + `${d.content_type || "no content type"}; signature headers seen: ${(d.signature.headers_seen || []).join(", ") || "none"}`),
        el("p", { class: "small dim" }, d.note)));
      return;
    }
    const headers = el("table", {}, el("tbody", {}, Object.entries(d.headers || {}).map(([k, v]) =>
      el("tr", {}, el("td", { class: "mono" }, k), el("td", { class: "mono" }, v)))));
    box.append(el("div", { class: "detail" }, when,
      el("p", {}, `auth: ${d.auth.configured}, accepted; signature: ${verdict(d.signature)}`),
      el("h3", {}, "headers (Authorization reduced to its scheme, configured secrets replaced)"), headers,
      el("h3", {}, "fields found"), jsonTree(d.fields_found || {}), el("h3", {}, "body"), jsonTree(d.body)));
  } catch (e) { box.append(el("p", { class: "empty" }, errorText(e))); }
}

async function loadDeliveries() {
  $("#hook-url").textContent = cfg.hook_url || "";
  try {
    const list = await api("/api/hooks/deliveries", { params: { limit: 50 } });
    const rows = list.deliveries.map((d) => el("tr", { class: "clickable", onclick: () => showDelivery(d.id) },
      el("td", { class: "nowrap" }, fmtDateTime(d.received_at)),
      el("td", {}, el("span", { class: `status ${d.auth.ok ? "good" : "failure"}` }, d.auth.ok ? "authenticated" : "rejected")),
      el("td", {}, verdict(d.signature)), el("td", { class: "excerpt" }, d.excerpt || d.note || "")));
    clear($("#deliveries")).append(table(["received", "auth", "signature", "body"], rows,
      "No deliveries yet. They arrive when a rule with the webhook channel fires."));
  } catch (e) { clear($("#deliveries")).append(el("p", { class: "empty" }, errorText(e))); }
}

async function loadRoundTrip() {
  const box = $("#round-trip");
  try {
    const list = await api("/api/voyages");
    const last = (list.runs || []).find((r) => r.voyage === "alert-round-trip");
    if (!last) {
      clear(box).append(el("p", {}, "No round trip has sailed yet. ", el("a", { href: "/stir" }, "Start one on the Stir page"),
        " (needs the captain's key)."));
      return;
    }
    renderTimeline(box, await api(`/api/voyages/${encodeURIComponent(last.id)}`));
  } catch (e) { clear(box).append(el("p", { class: "empty" }, errorText(e))); }
}

const filters = $("#instance-filter");
for (const f of ["all", "active", "resolved"]) {
  filters.append(el("button", { type: "button", value: f, "aria-pressed": String(f === filter), onclick: (ev) => {
    filter = f;
    filters.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === ev.target)));
    if (data) renderInstances();
  } }, f));
}
on("delivery", () => { loadDeliveries(); load(); });
on("voyage", (v) => { if (String(v.voyage || "").includes("alert") || v.step) loadRoundTrip(); });
load(); loadDeliveries(); loadRoundTrip();
setInterval(load, 30000);
