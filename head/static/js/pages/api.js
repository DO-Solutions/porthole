// API (/api): the full API trace ring, filterable, live, with the detail view of any call.
import { $, api, callDetail, clear, el, errorText, fmtMs, fmtTime, frame, on, shortPath } from "../porthole.js";

await frame();
const tbody = $("#calls");
const detail = $("#detail");

function matches(c) {
  const target = $("#target").value;
  const q = $("#q").value.trim().toLowerCase();
  return (!target || c.target === target) && (!q || String(c.path).toLowerCase().includes(q));
}

function row(c) {
  return el("tr", { class: "clickable", onclick: (ev) => {
    tbody.querySelectorAll("tr.selected").forEach((r) => r.classList.remove("selected"));
    ev.currentTarget.classList.add("selected");
    history.replaceState(null, "", `?call=${encodeURIComponent(c.id)}`);
    callDetail(detail, c.id);
  } }, el("td", { class: "nowrap" }, fmtTime(c.t)), el("td", {}, c.target), el("td", {}, c.method),
  el("td", {}, shortPath(c)), el("td", { class: "num" }, c.status ?? "error"), el("td", { class: "num" }, fmtMs(c.ms)),
  el("td", { class: "excerpt" }, c.excerpt || c.error || ""));
}

async function load() {
  try {
    const data = await api("/api/trace", { params: { limit: 500, target: $("#target").value, q: $("#q").value.trim() } });
    clear(tbody).append(...data.calls.map(row));
    $("#api-count").textContent = `${data.calls.length} call(s) shown; ${data.stats.last_minute} in the last minute`;
    if (!data.calls.length) tbody.append(el("tr", {}, el("td", { colspan: "7", class: "empty" }, "No calls match.")));
  } catch (e) { clear(tbody).append(el("tr", {}, el("td", { colspan: "7" }, errorText(e)))); }
}

let pending = null;
$("#target").addEventListener("change", load);
$("#q").addEventListener("input", () => { clearTimeout(pending); pending = setTimeout(load, 300); });
on("api_call", (c) => {
  if (!matches(c)) return;
  tbody.prepend(row({ ...c, excerpt: "" }));
  while (tbody.children.length > 500) tbody.lastChild.remove();
});
await load();
const wanted = new URL(location.href).searchParams.get("call");
if (wanted) callDetail(detail, wanted);
