import assert from "node:assert/strict";
import test from "node:test";
import {
  BRUSH_PARAM,
  BRUSH_TUPLE_SIGNAL,
  ZOOM_PARAM_PREFIX,
  brushChannels,
  buildInteractiveSpec,
  makeBrushBuffer,
  plotInteractivity,
  plotPngFilename,
  tupleDomains,
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
  if (result.mode === "full") assert.deepEqual(result.channels, ["y"]);
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
  if (result.mode === "full") assert.deepEqual(result.channels.sort(), ["x", "y"]);
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
  if (result.mode === "full") assert.deepEqual(result.channels, ["x"]);
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
  if (result.mode === "full") assert.deepEqual(result.channels, ["y"]);
});

test("plotInteractivity: domainRaw encodings drop out of full mode", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domainRaw: { signal: "[2, 8]" } } },
      y: { field: "y", type: "quantitative" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") assert.deepEqual(result.channels, ["y"]);
});

test("plotInteractivity: domainRaw on every axis degrades to tooltip", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domainRaw: { signal: "[2, 8]" } } },
      y: { field: "y", type: "quantitative", scale: { domainRaw: { signal: "[1, 9]" } } },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: piecewise axis leaves only the valid channel interactive", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: {
        field: "x",
        type: "quantitative",
        scale: { domain: [0, 5, 10], range: [0, 200, 400] },
      },
      y: { field: "y", type: "quantitative" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "full", channels: ["y"] });
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
    params: [{ name: ZOOM_PARAM_PREFIX, value: 1 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: derived-name collision on wiki_zoom_x downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" }, y: { field: "y", type: "quantitative" } },
    params: [{ name: `${ZOOM_PARAM_PREFIX}_x`, value: 42 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: derived-name collision on wiki_brush_tuple downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: BRUSH_TUPLE_SIGNAL, value: null }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: top-level dataset named wiki_zoom_x_store downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    datasets: { "wiki_zoom_x_store": [{ x: 1 }] },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: top-level dataset named wiki_brush_store downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    datasets: { "wiki_brush_store": [{ x: 1 }] },
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

test("plotInteractivity: user-authored bind:scales interval downgrades", () => {
  // R8F1: appended wiki_zoom_x would steal the single domainRaw binding from
  // this user selection. Downgrade rather than silently break the user's
  // interaction.
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: "user_pan", select: { type: "interval" }, bind: "scales" }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: user-authored unbound brush (interval without bind) downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: "user_brush", select: { type: "interval" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: user-authored point selection downgrades", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: "user_click", select: { type: "point" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: user scalar params (no select) stay in full mode", () => {
  const spec = {
    mark: "point",
    encoding: { x: { field: "x", type: "quantitative" } },
    params: [{ name: "user_toggle", value: true }],
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
});

test("plotInteractivity: legacy top-level `selection` key downgrades", () => {
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
    encoding: { x: { field: "x", type: "quantitative" }, y: { field: "y", type: "quantitative" } },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark errorbar degrades to tooltip", () => {
  const spec = {
    mark: { type: "errorbar", extent: "ci" },
    encoding: { x: { field: "x", type: "quantitative" }, y: { field: "y", type: "quantitative" } },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: composite mark errorband degrades to tooltip", () => {
  const spec = {
    mark: { type: "errorband" },
    encoding: { x: { field: "x", type: "quantitative" }, y: { field: "y", type: "quantitative" } },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
});

test("plotInteractivity: same-field-both-axes stays in full mode", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  };
  const result = plotInteractivity(spec);
  assert.equal(result.mode, "full");
  if (result.mode === "full") assert.deepEqual(result.channels.sort(), ["x", "y"]);
});

test("brushChannels: distinct fields → both channels", () => {
  assert.deepEqual(
    brushChannels(["x", "y"], { x: { field: "a" }, y: { field: "b" } }),
    ["x", "y"],
  );
});

test("brushChannels: same field on both axes → drop y (visible band matches applied zoom)", () => {
  // R8F2: if we projected y too, Vega-Lite would dedupe and render a
  // full-height band whose visual bounds don't match what x-only zoom does.
  // Dropping y makes the visible x-band match the x-only zoom on release.
  assert.deepEqual(
    brushChannels(["x", "y"], { x: { field: "v" }, y: { field: "v" } }),
    ["x"],
  );
});

test("brushChannels: single-channel full mode passes through", () => {
  assert.deepEqual(brushChannels(["x"], { x: { field: "a" } }), ["x"]);
  assert.deepEqual(brushChannels(["y"], { y: { field: "b" } }), ["y"]);
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

test("buildInteractiveSpec: full + armed injects per-channel zoom + single 2D brush", () => {
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
  assert.equal(params.length, 3, "wiki_zoom_x + wiki_zoom_y + wiki_brush");
  for (const channel of ["x", "y"] as const) {
    const zoom = params.find((p) => p.name === zoomParamName(channel))!;
    assert.equal(zoom.bind, "scales");
    assert.equal((zoom.select as Record<string, unknown>).bind, undefined);
    assert.deepEqual((zoom.select as Record<string, unknown>).encodings, [channel]);
  }
  const brush = params.find((p) => p.name === BRUSH_PARAM)!;
  assert.deepEqual((brush.select as Record<string, unknown>).encodings, ["x", "y"]);
  assert.equal((brush.select as Record<string, unknown>).translate, false);
});

test("buildInteractiveSpec: same-field brush projects x only (visible band matches zoom)", () => {
  const spec = {
    mark: "point",
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  };
  const out = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
  });
  const params = out.params as Array<Record<string, unknown>>;
  const brush = params.find((p) => p.name === BRUSH_PARAM)!;
  assert.deepEqual((brush.select as Record<string, unknown>).encodings, ["x"]);
  // Zoom still per-channel — same-field zoom works via wheel on each axis
  // independently.
  assert.ok(params.find((p) => p.name === zoomParamName("x")));
  assert.ok(params.find((p) => p.name === zoomParamName("y")));
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

test("buildInteractiveSpec: applies temporary zoom domains to encoding scales", () => {
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
  assert.equal((encoding.x.scale as Record<string, unknown>).padding, undefined);
  assert.deepEqual((encoding.y.scale as Record<string, unknown>).domain, [0, 5]);
});

test("buildInteractiveSpec: temporary domains remove conflicting scale modifiers", () => {
  for (const modifier of ["domainMin", "domainMax", "zero"] as const) {
    const spec = {
      mark: "point",
      encoding: {
        x: {
          field: "x",
          type: "quantitative",
          scale: { [modifier]: modifier === "zero" ? true : 0 },
        },
      },
    };
    const out = buildInteractiveSpec(spec, {
      interactivity: { mode: "full", channels: ["x"] },
      armed: false,
      domains: { x: [2, 8] },
    });
    const scale = ((out.encoding as Record<string, unknown>).x as Record<string, unknown>).scale as Record<string, unknown>;
    assert.deepEqual(scale.domain, [2, 8]);
    assert.equal(modifier in scale, false);
  }
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

test("tupleDomains: reads channel-tagged extents and normalizes ascending order", () => {
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

test("tupleDomains: preserves a live descending channel direction", () => {
  const domains = tupleDomains(
    {
      fields: [{ field: "price", channel: "x", type: "R" }],
      values: [[1, 10]],
    },
    ["x"],
    { x: "descending" },
  );
  assert.deepEqual(domains, { x: [10, 1] });
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

test("makeBrushBuffer: preserves the live descending direction", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(
    ["x"],
    (domains) => commits.push(domains),
    () => "descending",
  );
  buffer.onSignal({
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [[1, 10]],
  });
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [10, 1] }]);
});

test("makeBrushBuffer: pointerup with no signals is a no-op", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onPointerUp();
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
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
  buffer.onSignal(undefined);
  buffer.onSignal("garbage");
  buffer.onPointerUp();
  assert.deepEqual(commits, [{ x: [2, 8] }]);
});

test("makeBrushBuffer: null tuple clears a stale pending range", () => {
  const commits: unknown[] = [];
  const buffer = makeBrushBuffer(["x"], (d) => commits.push(d));
  buffer.onSignal({
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [[2, 8]],
  });
  buffer.onSignal(null);
  buffer.onPointerUp();
  assert.deepEqual(commits, []);
});

test("makeBrushBuffer: shrink-to-empty clears pending", () => {
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
