// The uPlot wrapper: one x array from start to end by step, gaps stay gaps, colors follow the fleet member,
// at most 8 drawn series, a crosshair tooltip, a legend for two or more series, and a table twin on every chart.
import { clear, el, fmtDateTime, regionUrl } from "./porthole.js";

const MAX_DRAWN = 8;

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function colorOf(s) { return s.slot ? cssVar(`--slot-${s.slot}`) : cssVar("--dim"); }

export function formatValue(v, unit, short = false) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  const n = Number(v);
  const fixed = (x, d) => x.toLocaleString(undefined, { maximumFractionDigits: d });
  switch (unit) {
    case "percent": return `${fixed(n, short ? 0 : 1)}%`;
    case "bytes": {
      const steps = ["B", "kB", "MB", "GB", "TB"];
      let i = 0; let x = Math.abs(n);
      while (x >= 1000 && i < steps.length - 1) { x /= 1000; i++; }
      return `${n < 0 ? "-" : ""}${fixed(x, x < 10 ? 2 : 1)} ${steps[i]}`;
    }
    case "seconds": return Math.abs(n) < 1 ? `${fixed(n * 1000, 0)} ms` : `${fixed(n, 2)} s`;
    case "ms": return Math.abs(n) >= 1000 ? `${fixed(n / 1000, 2)} s` : `${fixed(n, short ? 0 : 1)} ms`;
    case "per_second": return `${fixed(n, Math.abs(n) < 10 ? 2 : 1)}/s`;
    default: return fixed(n, Math.abs(n) < 10 ? 3 : 1);
  }
}

export function seriesLabel(s, payload) {
  const regions = new Set((payload.series || []).map((x) => x.region));
  return regions.size > 1 ? `${s.display} (${s.region})` : s.display;
}

export function align(payload) {
  const { start, end, step } = payload;
  const xs = [];
  for (let t = start; t <= end; t += step) xs.push(t);
  const ys = (payload.series || []).map((s) => {
    const arr = new Array(xs.length).fill(null);
    for (const [t, v] of s.points || []) {
      const k = Math.round((t - start) / step);
      if (k >= 0 && k < xs.length) arr[k] = v;
    }
    return arr;
  });
  return [xs, ys];
}

export function pick(series) {
  const order = [...series].sort((a, b) => (a.slot || 99) - (b.slot || 99) || String(a.display).localeCompare(b.display));
  return { drawn: order.slice(0, MAX_DRAWN), folded: order.slice(MAX_DRAWN) };
}

function tableTwin(payload, xs, ys, unit) {
  const series = payload.series || [];
  const head = el("tr", {}, el("th", {}, "time (UTC)"), series.map((s) => el("th", { class: "num" }, seriesLabel(s, payload))));
  const rows = [];
  for (let i = xs.length - 1; i >= 0 && rows.length < 60; i--) {
    rows.push(el("tr", {}, el("td", { class: "nowrap" }, fmtDateTime(new Date(xs[i] * 1000).toISOString())),
      ys.map((col) => el("td", { class: "num" }, formatValue(col[i], unit)))));
  }
  return el("div", { class: "table-wrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, rows)));
}

function legend(payload, drawn, ys, unit) {
  const box = el("div", { class: "chart-legend" });
  drawn.forEach((s) => {
    const col = ys[payload.series.indexOf(s)];
    const last = [...col].reverse().find((v) => v !== null && v !== undefined);
    box.append(el("span", {}, el("span", { class: "swatch", vars: { "--swatch": s.slot ? `var(--slot-${s.slot})` : "var(--dim)" } }),
      seriesLabel(s, payload), " ", el("span", { class: "value dim" }, last === undefined ? "no data" : formatValue(last, unit))));
  });
  return box;
}

function tooltipPlugin(tip, payload, drawn, unit) {
  return {
    hooks: {
      setCursor: [(u) => {
        const idx = u.cursor.idx;
        if (idx === null || idx === undefined || u.cursor.left < 0) { tip.classList.add("hidden"); return; }
        clear(tip).append(el("div", { class: "t" }, new Date(u.data[0][idx] * 1000).toLocaleString()));
        drawn.forEach((s, i) => {
          const v = u.data[i + 1][idx];
          tip.append(el("div", {}, el("span", { class: "swatch", vars: { "--swatch": s.slot ? `var(--slot-${s.slot})` : "var(--dim)" } }),
            el("strong", { class: "v" }, v === null || v === undefined ? "gap" : formatValue(v, unit)),
            el("span", { class: "dim" }, seriesLabel(s, payload))));
        });
        tip.classList.remove("hidden");
        const box = u.over.getBoundingClientRect();
        const host = tip.parentElement.getBoundingClientRect();
        const x = box.left - host.left + u.cursor.left + 14;
        const flip = x + tip.offsetWidth > host.width;
        tip.style.setProperty("left", `${flip ? x - tip.offsetWidth - 28 : x}px`);
        tip.style.setProperty("top", `${Math.max(0, box.top - host.top + u.cursor.top - 10)}px`);
      }],
    },
  };
}

function yRange(unit) {
  return (u, min, max) => {
    if (min === null || max === null) return [0, unit === "percent" ? 100 : 1];
    if (unit === "percent") return [Math.min(0, min), Math.max(100, max)];
    const lo = Math.min(0, min);
    return [lo, max === lo ? lo + 1 : max + (max - lo) * 0.08];
  };
}

function notices(payload) {
  const out = [];
  for (const [region, info] of Object.entries(payload.regions || {})) {
    if (info.error) out.push(`${region}: ${info.error.status ? `HTTP ${info.error.status}, ` : ""}${info.error.message}`);
  }
  if (payload.stale) out.push(`Served from cache: the Insights call budget is used up; fresh data in about ${payload.retry_in || 60} s.`);
  return out;
}

// Draw (or redraw) a range payload into el. Returns a handle with toggleTable().
export function drawRange(el0, payload, options = {}) {
  const unit = options.unit || payload.unit || "plain";
  const [xs, ys] = align(payload);
  const { drawn, folded } = pick(payload.series || []);
  if (el0._uplot) { el0._uplot.destroy(); el0._uplot = null; }
  if (el0._resize) { el0._resize.disconnect(); el0._resize = null; }
  clear(el0);
  el0.classList.remove("loading");
  const table = el("div", { class: "chart-table hidden" }, tableTwin(payload, xs, ys, unit));
  const handle = { payload, toggleTable() { table.classList.toggle("hidden"); return !table.classList.contains("hidden"); } };
  for (const text of notices(payload)) el0.append(el("p", { class: "chart-notice" }, text));
  if (!(payload.series || []).length) {
    el0.append(el("p", { class: "empty" }, options.empty || "Insights returned no series for this query."));
    return handle;
  }
  if (folded.length) el0.append(el("p", { class: "chart-notice" }, `+${folded.length} more (table view)`));
  if (typeof window.uPlot !== "function") {
    el0.append(el("p", { class: "chart-notice" }, "Charts are off because uPlot did not load; the table shows the data."), table);
    table.classList.remove("hidden");
    return handle;
  }
  const canvas = el("div", { class: "chart-canvas" });
  const tip = el("div", { class: "chart-tooltip hidden", role: "status" });
  el0.append(canvas, tip);
  if (drawn.length >= 2 && !options.spark) el0.append(legend(payload, drawn, ys, unit));
  el0.append(table);
  const surface = cssVar("--card"); const dim = cssVar("--dim");
  const font = `12px ${cssVar("--font") || "sans-serif"}`;
  const axis = { stroke: dim, font, grid: { stroke: cssVar("--grid-minor"), width: 1 },
    ticks: { stroke: cssVar("--grid-major"), width: 1, size: 4 } };
  const opts = {
    width: Math.max(200, el0.clientWidth || 600), height: options.height || (options.spark ? 70 : 240),
    legend: { show: false },
    scales: { x: { time: true }, y: { range: yRange(unit) } },
    series: [{}, ...drawn.map((s) => {
      const color = colorOf(s);
      const few = (s.points || []).length < 3;
      return { label: seriesLabel(s, payload), stroke: color, width: 2, spanGaps: false,
        points: { show: few, size: 8, width: 2, stroke: surface, fill: color } };
    })],
    axes: options.spark ? [{ show: false }, { show: false }]
      : [axis, { ...axis, size: 64, values: (u, splits) => splits.map((v) => formatValue(v, unit, true)) }],
    cursor: { drag: { x: false, y: false }, points: { size: 8, width: 2, stroke: surface },
      move: (u, left, top) => {
        const idx = u.posToIdx(left);
        return [idx === null ? left : Math.round(u.valToPos(u.data[0][idx], "x")), top];
      } },
    plugins: options.spark ? [] : [tooltipPlugin(tip, payload, drawn, unit)],
  };
  const data = [xs, ...drawn.map((s) => ys[payload.series.indexOf(s)])];
  el0._uplot = new window.uPlot(opts, data, canvas);
  if (typeof ResizeObserver === "function") {
    el0._resize = new ResizeObserver(() => {
      if (el0._uplot && el0.clientWidth) el0._uplot.setSize({ width: el0.clientWidth, height: opts.height });
    });
    el0._resize.observe(el0);
  }
  return handle;
}

// The row under a chart: table view, copy PromQL, and the matching Insights tab.
export function chartActions(box, getHandle, { promql, link, copy } = {}) {
  clear(box);
  box.append(el("button", { type: "button", onclick: (ev) => {
    const handle = getHandle();
    if (handle) ev.target.textContent = handle.toggleTable() ? "chart view" : "table view";
  } }, "table view"));
  if (promql) box.append(el("button", { type: "button", onclick: () => copy(promql(), "PromQL copied") }, "copy PromQL"));
  if (link && link.url) {
    // The href is refreshed on every click, so the link follows the region selector.
    const refresh = (ev) => ev.currentTarget.setAttribute("href", regionUrl(link));
    box.append(el("a", { class: "button", href: regionUrl(link), target: "_blank", rel: "noopener",
      onclick: refresh, onauxclick: refresh, title: link.verified ? "" : "link pattern not verified yet (see BUGS.md)" },
    `open in Insights${link.verified ? "" : " (unverified link)"}`));
  }
}
