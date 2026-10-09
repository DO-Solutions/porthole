// Brain (/brain): ask the deckhand, watch its session events stream in, and approve or deny its actions.
import { $, api, clear, el, errorText, fmtTime, frame, jsonTree, needsKey, toast } from "../porthole.js";

await frame();
const TYPES = ["status", "thinking", "message", "tool_call", "tool_result", "approval_request", "approval_resolved",
  "error", "done"];
const box = $("#events");
let source = null;
let session = null;
const calls = {};

// A 30-line allowlist renderer: paragraphs, "- " lists, **bold** and `code`; everything else is plain text.
function inline(text) {
  const out = [];
  for (const part of String(text).split(/(\*\*[^*]+\*\*|`[^`]+`)/)) {
    if (!part) continue;
    if (part.startsWith("**") && part.endsWith("**")) out.push(el("strong", {}, part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`")) out.push(el("code", {}, part.slice(1, -1)));
    else out.push(document.createTextNode(part));
  }
  return out;
}
function markdown(text) {
  const wrap = el("div", { class: "answer" });
  for (const block of String(text).split(/\n\s*\n/)) {
    const lines = block.split("\n");
    if (lines.every((l) => l.startsWith("- "))) wrap.append(el("ul", {}, lines.map((l) => el("li", {}, inline(l.slice(2))))));
    else wrap.append(el("p", {}, inline(lines.join(" "))));
  }
  return wrap;
}

function approvalCard(d) {
  const answer = (decision) => async () => {
    try {
      await api(`/api/brain/sessions/${encodeURIComponent(session)}/approvals/${encodeURIComponent(d.approval_id)}`,
        { method: "POST", captain: true, body: { decision } });
    } catch (e) { toast(errorText(e), "failure"); }
  };
  return el("div", { class: "event approval", id: `approval-${d.approval_id}` },
    el("strong", {}, `Approval needed: ${d.tool}`), el("p", {}, d.reason),
    jsonTree(d.args), el("p", { class: "small dim" }, `expires ${fmtTime(d.expires_at)}`),
    el("div", { class: "row" }, needsKey(el("button", { type: "button", class: "primary", onclick: answer("approve") }, "Approve")),
      needsKey(el("button", { type: "button", onclick: answer("deny") }, "Deny"))));
}

function render(ev) {
  const d = ev.data || {};
  if (ev.type === "status") return el("div", { class: "event status-line" }, d.text);
  if (ev.type === "thinking") return el("div", { class: "event status-line" }, el("em", {}, d.text));
  if (ev.type === "message") return el("div", { class: "event" }, markdown(d.text));
  if (ev.type === "tool_call") {
    const node = el("details", { class: "event" }, el("summary", {}, `tool call: ${d.tool}`), jsonTree(d.args));
    calls[d.call_id] = node;
    return node;
  }
  if (ev.type === "tool_result") {
    const node = calls[d.call_id];
    const links = (d.trace_ids || []).map((id) => el("a", { href: `/api?call=${encodeURIComponent(id)}` }, id));
    const line = el("p", { class: "small" }, el("span", { class: `status ${d.ok ? "good" : "failure"}` }, d.ok ? "ok" : "failed"),
      ` ${d.summary} `, links);
    if (node) { node.querySelector("summary").append(` (${d.ok ? "ok" : "failed"})`); node.append(line); return null; }
    return el("div", { class: "event" }, line);
  }
  if (ev.type === "approval_request") return approvalCard(d);
  if (ev.type === "approval_resolved") {
    const card = document.getElementById(`approval-${d.approval_id}`);
    if (card) card.querySelectorAll("button").forEach((b) => { b.disabled = true; delete b.dataset.needsKey; });
    return el("div", { class: "event status-line" }, `${d.decision} by ${d.actor} at ${fmtTime(d.t)}`);
  }
  if (ev.type === "error") return el("div", { class: "event error" }, `error: ${d.message}`);
  return el("div", { class: "event status-line" }, `done: ${d.summary || ""}`);
}

function follow(id) {
  if (source) source.close();
  session = id;
  source = new EventSource(`/api/brain/sessions/${encodeURIComponent(id)}/events`);
  for (const type of TYPES) {
    source.addEventListener(type, (msg) => {
      const node = render(JSON.parse(msg.data));
      if (node) box.append(node);
      if (type === "done" || type === "error") source.close();
    });
  }
}

async function askQuestion(question) {
  if (!question.trim()) return;
  clear(box).append(el("div", { class: "event status-line" }, `you asked: ${question}`));
  try { follow((await api("/api/brain/sessions", { method: "POST", body: { question } })).id); }
  catch (e) { box.append(el("div", { class: "event error" }, errorText(e))); }
}

$("#ask").addEventListener("click", () => askQuestion($("#question").value));
$("#question").addEventListener("keydown", (ev) => { if (ev.key === "Enter") askQuestion($("#question").value); });
try {
  const info = await api("/api/brain/info");
  $("#answering").textContent = `Answering: ${info.label}`;
  for (const q of info.suggested) {
    $("#suggestions").append(el("button", { type: "button", onclick: () => { $("#question").value = q; askQuestion(q); } }, q));
  }
  for (const t of info.tools) {
    $("#tools").append(el("li", {}, el("code", {}, t.name), " ", t.description, " ",
      t.needs_approval ? el("span", { class: "badge" }, "asks first") : null));
  }
  if (info.backend === "off") { $("#ask").disabled = true; $("#answering").textContent = "The Brain is off on this server."; }
} catch (e) { $("#answering").textContent = errorText(e); }
