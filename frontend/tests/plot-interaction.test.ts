import assert from "node:assert/strict";
import test from "node:test";
import {
  BRUSH_PARAM,
  ZOOM_PARAM,
  buildInteractiveSpec,
  makeBrushBuffer,
  plotInteractivity,
  plotPngFilename,
  selectionDomains,
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
    assert.deepEqual(result.fields, { y: "count" });
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
    assert.equal(result.fields.x, "price");
    assert.equal(result.fields.y, "ts");
  }
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

test("buildInteractiveSpec: full + armed adds zoom and brush params", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"], fields: { x: "x", y: "y" } },
    armed: true,
    brushColor: "#abc",
  });
  const params = out.params as Array<Record<string, unknown>>;
  assert.equal(params.length, 2);
  const zoom = params.find((p) => p.name === ZOOM_PARAM)!;
  // bind:scales MUST sit on the parameter, not inside select — Vega-Lite v5/v6
  // silently ignores nested bind so pan/wheel would degrade to drawing a
  // selection rectangle instead of moving the domains.
  assert.equal(zoom.bind, "scales");
  assert.equal((zoom.select as Record<string, unknown>).bind, undefined);
  assert.equal((zoom.select as Record<string, unknown>).zoom, "wheel!");
  const brush = params.find((p) => p.name === BRUSH_PARAM)!;
  assert.equal((brush.select as Record<string, unknown>).translate, false);
  assert.equal(((brush.select as Record<string, unknown>).mark as Record<string, unknown>).stroke, "#abc");
});

test("buildInteractiveSpec: full but unarmed omits params (inline tooltip mode)", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x"], fields: { x: "x" } },
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
    interactivity: { mode: "full", channels: ["x", "y"], fields: { x: "x", y: "y" } },
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
    interactivity: { mode: "full", channels: ["x"], fields: { x: "x" } },
    armed: true,
    domains: { x: [0, 1] },
  });
  assert.equal(JSON.stringify(spec), before);
});

test("selectionDomains: reads field-keyed extents and sorts them", () => {
  const domains = selectionDomains(
    { price: [40, 12], date: [1_700_000_000_000, 1_710_000_000_000] },
    { x: "price", y: "date" },
  );
  assert.deepEqual(domains, {
    x: [12, 40],
    y: [1_700_000_000_000, 1_710_000_000_000],
  });
});

test("selectionDomains: rejects zero-width extents and non-finite values", () => {
  assert.equal(selectionDomains({ price: [5, 5] }, { x: "price" }), null);
  assert.equal(selectionDomains({ price: [Number.NaN, 3] }, { x: "price" }), null);
  assert.equal(selectionDomains({ price: [1] }, { x: "price" }), null);
});

test("selectionDomains: returns null when nothing matches known fields", () => {
  assert.equal(selectionDomains({ other: [1, 2] }, { x: "price" }), null);
  assert.equal(selectionDomains(null, { x: "price" }), null);
});

test("plotInteractivity: aggregate encodings downgrade the channel", () => {
  const spec = {
    mark: "bar",
    encoding: {
      x: { field: "category", type: "nominal" },
      y: { field: "revenue", type: "quantitative", aggregate: "sum" },
    },
  };
  // Both channels reject full-mode projection (nominal x, aggregate y) → tooltip.
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
    assert.deepEqual(result.fields, { x: "price" });
  }
});

test("plotInteractivity: scale:null encodings drop out of full mode", () => {
  // scale:null is a valid Vega-Lite encoding (used for raw pixel positioning,
  // custom layers, etc). Injecting an interval projection on such a channel
  // throws "Cannot read properties of undefined (reading get)" at compile.
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
    assert.deepEqual(result.fields, { y: "y" });
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
  // A timeUnit encoding compiles the selection signal against the derived
  // field name (yearmonth_ts) — mapping back to source `ts` would silently
  // miss the extent, so drop it out of full mode entirely.
  const spec = {
    mark: "line",
    encoding: {
      x: { field: "ts", type: "temporal", timeUnit: "yearmonth" },
      y: { field: "count", type: "quantitative", aggregate: "count" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: existing wiki_zoom param collision downgrades to tooltip", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: ZOOM_PARAM, select: { type: "interval" } }],
  };
  // Would have been full — but injecting our params would throw Vega's
  // duplicate-signal check, so we render tooltip-only instead.
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: existing wiki_brush param collision downgrades to tooltip", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
    },
    params: [{ name: BRUSH_PARAM, select: { type: "interval" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: unrelated params leave full mode intact", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
    },
    params: [{ name: "userToggle", value: true }],
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
});

test("makeBrushBuffer: commits only on pointerup, always the latest value", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer({ x: "x" }, (d) => commits.push(d));
  buffer.onSignal({ x: [0, 1] });
  buffer.onSignal({ x: [0, 5] });
  buffer.onSignal({ x: [0, 12] });
  assert.deepEqual(commits, []);
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 12] }]);
});

test("makeBrushBuffer: pointerup with no signals is a no-op", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer({ x: "x" }, (d) => commits.push(d));
  buffer.onPointerUp();
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
});

test("makeBrushBuffer: multiple gestures each commit once", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer({ x: "x" }, (d) => commits.push(d));
  buffer.onSignal({ x: [0, 1] });
  buffer.onPointerUp();
  buffer.onPointerUp(); // second pointerup with no new signal — do nothing
  buffer.onSignal({ x: [4, 8] });
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 1] }, { x: [4, 8] }]);
});

test("makeBrushBuffer: signals that don't parse to a domain are ignored", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer({ x: "x" }, (d) => commits.push(d));
  buffer.onSignal({ x: [5, 5] }); // zero-width — rejected
  buffer.onSignal(null);
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
