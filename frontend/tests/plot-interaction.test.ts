import assert from "node:assert/strict";
import test from "node:test";
import {
  BRUSH_PARAM_PREFIX,
  ZOOM_PARAM_PREFIX,
  brushParamName,
  buildInteractiveSpec,
  extentFromSignal,
  makeBrushBuffer,
  plotInteractivity,
  plotPngFilename,
  zoomParamName,
} from "../src/plot-interaction.ts";

test("plotInteractivity: no mark → static", () => {
  assert.deepEqual(plotInteractivity({ data: { values: [] } }), { mode: "static" });
});

test("plotInteractivity: composite spec → static", () => {
  const spec = { mark: "line", layer: [{ mark: "line" }], encoding: { x: { field: "a", type: "quantitative" } } };
  assert.deepEqual(plotInteractivity(spec), { mode: "static" });
});

test("plotInteractivity: mark with no encoding → static", () => {
  assert.deepEqual(plotInteractivity({ mark: "bar" }), { mode: "static" });
});

test("plotInteractivity: ordinal-only axes → tooltip", () => {
  const spec = {
    mark: "bar",
    encoding: {
      x: { field: "category", type: "nominal" },
      y: { field: "n", type: "ordinal" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: binned quantitative axis is not zoomable", () => {
  const spec = {
    mark: "bar",
    encoding: {
      x: { field: "n", type: "quantitative", bin: true },
      y: { field: "count", type: "quantitative" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") {
    assert.deepEqual(result.channels, ["y"]);
  }
});

test("plotInteractivity: continuous x + temporal y → full both channels", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "price", type: "quantitative" },
      y: { field: "ts", type: "temporal" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") {
    assert.deepEqual(result.channels.sort(), ["x", "y"]);
  }
});

test("plotInteractivity: aggregate encodings downgrade the channel", () => {
  const spec = {
    mark: "bar",
    encoding: {
      x: { field: "category", type: "nominal" },
      y: { field: "revenue", type: "quantitative", aggregate: "sum" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: aggregate on one channel leaves the other zoomable", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "price", type: "quantitative" },
      y: { field: "revenue", type: "quantitative", aggregate: "mean" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") {
    assert.deepEqual(result.channels, ["x"]);
  }
});

test("plotInteractivity: scale:null encodings drop out of full mode", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative", scale: null },
      y: { field: "y", type: "quantitative" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") {
    assert.deepEqual(result.channels, ["y"]);
  }
});

test("plotInteractivity: all-scale-null degrades to tooltip", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative", scale: null },
      y: { field: "y", type: "quantitative", scale: null },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: timeUnit encodings degrade to tooltip", () => {
  const spec = {
    mark: "line",
    encoding: {
      x: { field: "ts", type: "temporal", timeUnit: "yearmonth" },
      y: { field: "count", type: "quantitative", aggregate: "count" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: exact wiki_zoom param collision downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: ZOOM_PARAM_PREFIX, select: { type: "interval" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: derived-name collision on wiki_zoom_x downgrades", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${ZOOM_PARAM_PREFIX}_x`, value: 42 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: derived-name collision on wiki_brush_x downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: `${BRUSH_PARAM_PREFIX}_x`, value: null }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: top-level dataset named wiki_zoom_x_store downgrades", () => {
  // Vega compiles `wiki_zoom_x_store` as the selection store for our
  // injected wiki_zoom_x param. If a user top-level dataset already uses
  // that name, the first selection update replaces its rows with the
  // selection tuple — the chart source goes empty.
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    datasets: { "wiki_zoom_x_store": [{ x: 1 }] },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: top-level dataset named wiki_brush_x_store downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    datasets: { "wiki_brush_x_store": [{ x: 1 }] },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: data.name matching reserved prefix downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    data: { name: "wiki_zoom_foo", values: [{ x: 1 }] },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: legacy top-level `selection` key downgrades", () => {
  // Vega-Lite 6 still accepts the deprecated top-level `selection` block. If
  // one exists, the compiler compiles those selections and DROPS every
  // injected `params` entry — full mode would advertise pan/wheel/brush
  // with no handlers.
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    selection: { legacy_pan: { type: "interval", bind: "scales" } },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark boxplot degrades to tooltip", () => {
  const spec = {
    mark: "boxplot",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark errorbar degrades to tooltip", () => {
  const spec = {
    mark: { type: "errorbar", extent: "ci" },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark errorband degrades to tooltip", () => {
  const spec = {
    mark: { type: "errorband" },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: same-field-both-axes stays in full mode (per-channel params handle it)", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") {
    assert.deepEqual(result.channels.sort(), ["x", "y"]);
  }
});

test("plotInteractivity: unrelated params leave full mode intact", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: "userToggle", value: true }],
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
});

test("buildInteractiveSpec: static passes through unchanged", () => {
  const spec = { layer: [{ mark: "line" }] };
  const out = buildInteractiveSpec(spec, { interactivity: { mode: "static" }, armed: true });
  assert.strictEqual(out, spec);
});

test("buildInteractiveSpec: tooltip mode injects tooltip on string mark", () => {
  const spec = { mark: "bar", encoding: { x: { field: "c", type: "nominal" } } };
  const out = buildInteractiveSpec(spec, { interactivity: { mode: "tooltip" }, armed: false });
  assert.deepEqual(out.mark, { type: "bar", tooltip: true });
  assert.equal(out.params, undefined);
});

test("buildInteractiveSpec: preserves author-specified tooltip config", () => {
  const spec = { mark: { type: "bar", tooltip: { content: "data" } }, encoding: {} };
  const out = buildInteractiveSpec(spec, { interactivity: { mode: "tooltip" }, armed: false });
  assert.deepEqual((out.mark as Record<string, unknown>).tooltip, { content: "data" });
});

test("buildInteractiveSpec: full + armed injects one zoom and one brush per channel", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
    brushColor: "#abc",
  });
  const params = out.params as Array<Record<string, unknown>>;
  assert.equal(params.length, 4, "four params: zoom_x, brush_x, zoom_y, brush_y");
  for (const channel of ["x", "y"] as const) {
    const zoom = params.find((p) => p.name === zoomParamName(channel))!;
    assert.equal(zoom.bind, "scales", `${channel} zoom bind lives at param level`);
    assert.equal((zoom.select as Record<string, unknown>).bind, undefined);
    assert.deepEqual((zoom.select as Record<string, unknown>).encodings, [channel]);
    const brush = params.find((p) => p.name === brushParamName(channel))!;
    assert.equal((brush.select as Record<string, unknown>).translate, false);
    assert.deepEqual((brush.select as Record<string, unknown>).encodings, [channel]);
    assert.equal(((brush.select as Record<string, unknown>).mark as Record<string, unknown>).stroke, "#abc");
  }
});

test("buildInteractiveSpec: full but unarmed omits params (inline tooltip mode)", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x"] },
    armed: false,
  });
  assert.equal(out.params, undefined);
  assert.deepEqual(out.mark, { type: "point", tooltip: true });
});

test("buildInteractiveSpec: applies persistent zoom domains to encoding scales", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative", scale: { padding: 4 } },
      y: { field: "y", type: "quantitative" },
    },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
    domains: { x: [10, 20], y: [0, 5] },
  });
  const encoding = out.encoding as Record<string, Record<string, unknown>>;
  assert.deepEqual((encoding.x.scale as Record<string, unknown>).domain, [10, 20]);
  assert.equal((encoding.x.scale as Record<string, unknown>).padding, 4);
  assert.deepEqual((encoding.y.scale as Record<string, unknown>).domain, [0, 5]);
});

test("buildInteractiveSpec: does not mutate input", () => {
  const spec = {
    mark: "line",
    encoding: { x: { field: "x", type: "quantitative" } },
  };
  const before = JSON.stringify(spec);
  buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x"] },
    armed: true,
    domains: { x: [0, 1] },
  });
  assert.equal(JSON.stringify(spec), before);
});

test("buildInteractiveSpec: single-channel full mode injects only that channel's params", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x"] },
    armed: true,
  });
  const params = out.params as Array<Record<string, unknown>>;
  assert.equal(params.length, 2);
  assert.ok(params.every((p) => (p.name as string).endsWith("_x")));
});

test("extentFromSignal: reads the first array value regardless of key", () => {
  assert.deepEqual(extentFromSignal({ "v": [2, 8] }), [2, 8]);
  // Nested field path — the escaped key is opaque to us, but we don't need to
  // read it because 1D signals only ever have one entry.
  assert.deepEqual(extentFromSignal({ "a.b": [3, 7] }), [3, 7]);
  assert.deepEqual(extentFromSignal({ "some\\.field": [-1, 4] }), [-1, 4]);
});

test("extentFromSignal: reverses swapped low/high", () => {
  assert.deepEqual(extentFromSignal({ "v": [8, 2] }), [2, 8]);
});

test("extentFromSignal: empty and malformed → null", () => {
  assert.equal(extentFromSignal({}), null);
  assert.equal(extentFromSignal(null), null);
  assert.equal(extentFromSignal({ "v": [] }), null);
  assert.equal(extentFromSignal({ "v": [5, 5] }), null);
  assert.equal(extentFromSignal({ "v": [Number.NaN, 3] }), null);
  assert.equal(extentFromSignal([1, 2]), null);
});

test("makeBrushBuffer: commits only on pointerup, always the latest per-channel extent", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x", "y"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [0, 1] });
  buffer.onChannelSignal("x", { v: [0, 5] });
  buffer.onChannelSignal("y", { v: [1, 4] });
  buffer.onChannelSignal("x", { v: [0, 12] });
  assert.deepEqual(commits, []);
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 12], y: [1, 4] }]);
});

test("makeBrushBuffer: rejects signals for channels outside the allowlist", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [0, 5] });
  buffer.onChannelSignal("y" as never, { v: [0, 5] });
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 5] }]);
});

test("makeBrushBuffer: pointerup with no signals is a no-op", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onPointerUp();
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
});

test("makeBrushBuffer: multiple gestures each commit once", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [0, 1] });
  buffer.onPointerUp();
  buffer.onPointerUp();
  buffer.onChannelSignal("x", { v: [4, 8] });
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 1] }, { x: [4, 8] }]);
});

test("makeBrushBuffer: shape-less signal noise is ignored", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [2, 8] });
  buffer.onChannelSignal("x", null);         // shape-less: keep pending
  buffer.onChannelSignal("x", undefined);    // ditto
  buffer.onChannelSignal("x", "garbage");    // ditto
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [2, 8] }]);
});

test("makeBrushBuffer: shrink-to-empty clears that channel's pending", () => {
  // Reviewer case: user drags x out, y stays selected, then drags x back to
  // the anchor before releasing. Pointerup MUST commit y only, not the
  // earlier intermediate x range.
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x", "y"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [2, 8] });
  buffer.onChannelSignal("y", { v: [1, 4] });
  buffer.onChannelSignal("x", { v: [5, 5] }); // shrunk on x only
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ y: [1, 4] }]);
});

test("makeBrushBuffer: onCancel clears pending across an aborted gesture", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x", "y"], (d) => commits.push(d));
  buffer.onChannelSignal("x", { v: [2, 8] });
  buffer.onChannelSignal("y", { v: [1, 4] });
  buffer.onCancel();
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
});

test("plotPngFilename: sanitizes and defaults", () => {
  assert.equal(plotPngFilename(null), "plot.png");
  assert.equal(plotPngFilename(""), "plot.png");
  assert.equal(plotPngFilename("  "), "plot.png");
  assert.equal(plotPngFilename("Revenue / 2025"), "Revenue-2025.png");
  assert.equal(plotPngFilename("---leading"), "leading.png");
  assert.equal(plotPngFilename("chart.v1"), "chart.v1.png");
});
