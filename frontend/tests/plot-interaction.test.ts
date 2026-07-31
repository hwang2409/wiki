import assert from "node:assert/strict";
import test from "node:test";
import {
  BRUSH_PARAM,
  ZOOM_PARAM,
  buildInteractiveSpec,
  makeBrushBuffer,
  plotInteractivity,
  plotPngFilename,
  tupleDomains,
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
    interactivity: { mode: "full", channels: ["x", "y"] },
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

test("tupleDomains: reads channel-tagged extents and sorts them", () => {
  const domains = tupleDomains(
    {
      unit: "",
      fields: [
        { field: "price", channel: "x", type: "R" },
        { field: "date", channel: "y", type: "R" },
      ],
      values: [[40, 12], [1_700_000_000_000, 1_710_000_000_000]],
    },
    ["x", "y"],
  );
  assert.deepEqual(domains, {
    x: [12, 40],
    y: [1_700_000_000_000, 1_710_000_000_000],
  });
});

test("tupleDomains: nested field path survives (channel is authoritative, not field name)", () => {
  const domains = tupleDomains(
    {
      unit: "",
      fields: [
        { field: "a.b", channel: "x", type: "R" },
        { field: "a[0]", channel: "y", type: "R" },
      ],
      values: [[2, 8], [3, 7]],
    },
    ["x", "y"],
  );
  assert.deepEqual(domains, { x: [2, 8], y: [3, 7] });
});

test("tupleDomains: same field on both axes yields two distinct extents", () => {
  // The user-facing `wiki_brush` signal collapses this to {v: [x-extent]},
  // losing the y-extent. The tuple preserves both because the channel is on
  // the metadata entry, not the map key.
  const domains = tupleDomains(
    {
      unit: "",
      fields: [
        { field: "v", channel: "x", type: "R" },
        { field: "v", channel: "y", type: "R" },
      ],
      values: [[2, 8], [3, 7]],
    },
    ["x", "y"],
  );
  assert.deepEqual(domains, { x: [2, 8], y: [3, 7] });
});

test("tupleDomains: rejects zero-width extents and non-finite values", () => {
  const empty = tupleDomains(
    {
      unit: "",
      fields: [
        { field: "x", channel: "x", type: "R" },
        { field: "y", channel: "y", type: "R" },
      ],
      values: [[5, 5], [Number.NaN, 3]],
    },
    ["x", "y"],
  );
  assert.equal(empty, null);
});

test("tupleDomains: ignores channels outside the allowlist", () => {
  const domains = tupleDomains(
    {
      unit: "",
      fields: [
        { field: "x", channel: "x", type: "R" },
        { field: "z", channel: "color", type: "R" },
      ],
      values: [[0, 1], [0, 2]],
    },
    ["x"],
  );
  assert.deepEqual(domains, { x: [0, 1] });
});

test("tupleDomains: returns null for malformed tuples", () => {
  assert.equal(tupleDomains(null, ["x"]), null);
  assert.equal(tupleDomains({ fields: [], values: [] }, ["x"]), null);
  assert.equal(
    tupleDomains({ fields: [{ field: "x", channel: "x", type: "R" }], values: [] }, ["x"]),
    null,
  );
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

test("plotInteractivity: existing wiki_zoom param collision downgrades to tooltip", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: ZOOM_PARAM, select: { type: "interval" } }],
  };
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

test("plotInteractivity: derived-name collision on wiki_zoom_x downgrades", () => {
  // Vega-Lite compiles a wiki_zoom_x signal from our injected wiki_zoom
  // param. A user scalar param with the same name would trip Vega's
  // duplicate-signal check at parse time, breaking a previously-working plot.
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${ZOOM_PARAM}_x`, value: 42 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: derived-name collision on wiki_brush_tuple downgrades", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
    },
    params: [{ name: `${BRUSH_PARAM}_tuple`, value: null }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark boxplot degrades to tooltip", () => {
  // Vega-Lite strips interval selections from composite marks. Full mode
  // would enable Reset and hint drag/wheel/shift-drag with no live wiring.
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

test("plotInteractivity: same-field-both-axes stays in full mode (aliasing handles it)", () => {
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
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  const wrap = (values: number[][]) => ({
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values,
  });
  buffer.onSignal(wrap([[0, 1]]));
  buffer.onSignal(wrap([[0, 5]]));
  buffer.onSignal(wrap([[0, 12]]));
  assert.deepEqual(commits, []);
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 12] }]);
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
  const wrap = (extent: number[]) => ({
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [extent],
  });
  buffer.onSignal(wrap([0, 1]));
  buffer.onPointerUp();
  buffer.onPointerUp();
  buffer.onSignal(wrap([4, 8]));
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [0, 1] }, { x: [4, 8] }]);
});

test("makeBrushBuffer: shape-less signal noise is ignored", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  const wrap = (extent: number[]) => ({
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [extent],
  });
  buffer.onSignal(wrap([2, 8]));
  buffer.onSignal(null);        // shape-less: ignore, keep pending
  buffer.onSignal(undefined);   // ditto
  buffer.onSignal("garbage");   // ditto
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [2, 8] }]);
});

test("makeBrushBuffer: shrink-to-empty clears pending so pointerup commits nothing", () => {
  // Reviewer case: user shift-drags out to a valid extent, then drags back
  // to the anchor before releasing. The final signal is a well-formed tuple
  // with zero-width values, and pointerup MUST NOT commit the earlier
  // intermediate range.
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  const tupleWith = (values: number[][]) => ({
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values,
  });
  buffer.onSignal(tupleWith([[2, 8]]));
  buffer.onSignal(tupleWith([[2, 5]]));
  buffer.onSignal(tupleWith([[5, 5]])); // shrunk back to a point
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
});

test("makeBrushBuffer: onCancel clears pending across an aborted gesture", () => {
  // Reviewer case: pointercancel between drag and release. A later unrelated
  // pointerup must not commit the stale extent.
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onSignal({
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [[2, 8]],
  });
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
