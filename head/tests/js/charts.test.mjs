// Unit tests for the pure parts of charts.js: gap alignment, series order and folding, labels and units.
// Run with `node --test "head/tests/js/*.test.mjs"`; head/tests/test_static.py runs it when node is installed.
import assert from "node:assert/strict";
import test from "node:test";

import { align, formatValue, pick, seriesLabel } from "../../static/js/charts.js";

test("align builds one x array by step and leaves gaps as null", () => {
  const payload = { start: 0, end: 300, step: 60, series: [{ points: [[0, 1], [120, 3], [300, 6]] }, { points: [] }] };
  const [xs, ys] = align(payload);
  assert.deepEqual(xs, [0, 60, 120, 180, 240, 300]);
  assert.deepEqual(ys[0], [1, null, 3, null, null, 6]);
  assert.deepEqual(ys[1], [null, null, null, null, null, null]);
});

test("align snaps off-grid samples to the nearest step", () => {
  const [, ys] = align({ start: 0, end: 120, step: 60, series: [{ points: [[61, 5], [119, 7]] }] });
  assert.deepEqual(ys[0], [null, 5, 7]);
});

test("pick draws fleet members first, by slot, and folds past eight", () => {
  const series = Array.from({ length: 11 }, (_, i) => ({ display: `s${i}`, slot: i < 5 ? 5 - i : null }));
  const { drawn, folded } = pick(series);
  assert.equal(drawn.length, 8);
  assert.equal(folded.length, 3);
  assert.deepEqual(drawn.slice(0, 5).map((s) => s.slot), [1, 2, 3, 4, 5]);
  assert.ok(folded.every((s) => s.slot === null));
});

test("a series keeps its slot whatever else is on the chart", () => {
  const t2 = { display: "tentacle-2", slot: 2 };
  const alone = pick([t2]).drawn[0];
  const crowded = pick([{ display: "tentacle-1", slot: 1 }, t2, { display: "other", slot: null }]).drawn[1];
  assert.equal(alone.slot, 2);
  assert.equal(crowded.slot, 2);
});

test("labels name the region only when regions mix", () => {
  const one = { series: [{ display: "tentacle-1", region: "tor1" }] };
  const both = { series: [{ display: "tentacle-1", region: "tor1" }, { display: "tentacle-3", region: "syd1" }] };
  assert.equal(seriesLabel(one.series[0], one), "tentacle-1");
  assert.equal(seriesLabel(both.series[1], both), "tentacle-3 (syd1)");
});

test("units follow the metric name heuristics", () => {
  assert.equal(formatValue(61.43, "percent"), "61.4%");
  assert.equal(formatValue(1500000, "bytes"), "1.5 MB");
  assert.equal(formatValue(0.35, "seconds"), "350 ms");
  assert.equal(formatValue(2500, "ms"), "2.5 s");
  assert.equal(formatValue(1.3, "per_second"), "1.3/s");
  assert.equal(formatValue(null, "percent"), "");
});
