// Compile-level and vega-runtime assertions for WIKI-194. These push the
// transformed spec through the actual vega-lite compiler AND the vega runtime
// so a subtle mistake (e.g. bind:scales nested inside select, or a namespace
// collision Vega only catches at parse time) can't hide behind a shape-only
// unit test.
import assert from "node:assert/strict";
import test from "node:test";
import { compile } from "vega-lite";
import { View, parse } from "vega";
import {
  BRUSH_PARAM,
  BRUSH_TUPLE_SIGNAL,
  ZOOM_PARAM,
  buildInteractiveSpec,
  plotInteractivity,
  tupleDomains,
} from "../src/plot-interaction.ts";

function transform(spec: Record<string, unknown>): Record<string, unknown> {
  const interactivity = plotInteractivity(spec);
  return buildInteractiveSpec(spec, {
    interactivity,
    armed: true,
    brushColor: "#333333",
  }) as Record<string, unknown>;
}

const CONTINUOUS: Record<string, unknown> = {
  mark: "point",
  data: { values: [{ x: 1, y: 1 }, { x: 4, y: 3 }] },
  encoding: {
    x: { field: "x", type: "quantitative" },
    y: { field: "y", type: "quantitative" },
  },
};

test("compile: full-mode spec compiles cleanly with domainRaw bound on every selected scale", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const scales = (output.spec.scales ?? []) as Array<Record<string, unknown>>;
  const zoomable = scales.filter((s) => s.name === "x" || s.name === "y");
  assert.equal(zoomable.length, 2, "both x and y scales are present");
  for (const scale of zoomable) {
    const domainRaw = scale.domainRaw as Record<string, unknown> | undefined;
    // If bind:"scales" is nested inside select, Vega-Lite drops it silently
    // and domainRaw is missing — pan/wheel then move a rectangle instead of
    // the scales. This assertion is the earliest signal that the parameter
    // is authored correctly.
    assert.ok(domainRaw, `scale "${scale.name as string}" is missing domainRaw`);
    assert.equal(
      domainRaw.signal,
      `${ZOOM_PARAM}["${scale.name as string}"]`,
      `scale "${scale.name as string}" domainRaw signal must reference ${ZOOM_PARAM}`,
    );
  }
});

test("compile: pan and zoom translation signals are wired to the compiled scales", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const signals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  const zoomX = signals.find((s) => s.name === `${ZOOM_PARAM}_x`);
  const zoomY = signals.find((s) => s.name === `${ZOOM_PARAM}_y`);
  assert.ok(zoomX && zoomY, "per-channel zoom signals must exist");
  const zoomOn = JSON.stringify(zoomX!.on ?? []);
  assert.match(zoomOn, /panLinear\(/);
  assert.match(zoomOn, /zoomLinear\(domain\(\\?"x\\?"\)/);
});

test("compile: brush selection compiles the store separately from the zoom param", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const signals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  assert.ok(signals.some((s) => s.name === BRUSH_PARAM), "brush signal must compile");
  assert.ok(signals.some((s) => s.name === ZOOM_PARAM), "zoom signal must compile");
  assert.ok(signals.some((s) => s.name === BRUSH_TUPLE_SIGNAL), "brush tuple signal must compile");
});

test("compile: scale:null encoding drops out and the surviving spec still compiles", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: null },
      y: { field: "y", type: "quantitative" },
    },
  };
  const interactivity = plotInteractivity(spec);
  assert.equal(interactivity.mode, "full");
  if (interactivity.mode === "full") {
    assert.deepEqual(interactivity.channels, ["y"]);
  }
  const interactive = transform(spec);
  assert.doesNotThrow(() => compile(interactive as never));
});

test("compile: all-scale-null spec degrades to tooltip mode and stays compilable", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: null },
      y: { field: "y", type: "quantitative", scale: null },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => compile(interactive as never));
});

async function renderHeadless(spec: Record<string, unknown>): Promise<View> {
  const compiled = compile(spec as never).spec;
  const runtime = parse(compiled);
  const view = new View(runtime, { renderer: "none" as never });
  await view.runAsync();
  return view;
}

test("runtime: wheel zoom shrinks the scale domain around the anchor", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
  });
  const view = await renderHeadless(interactive);
  const initial = view.scale("x").domain() as [number, number];
  assert.deepEqual(initial, [0, 10]);
  view
    .signal(`${ZOOM_PARAM}_zoom_anchor`, { x: 5, y: 5 })
    .signal(`${ZOOM_PARAM}_zoom_delta`, 2)
    .run();
  const after = view.scale("x").domain() as [number, number];
  assert.notDeepEqual(after, initial, "wheel-zoom must move the x domain");
  assert.ok(after[0] < initial[0] && after[1] > initial[1], "delta:2 zooms out around anchor 5");
  await view.finalize();
});

test("runtime: drag translation shifts the scale domain along the drag axis", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const initial = view.scale("x").domain() as [number, number];
  view
    .signal(`${ZOOM_PARAM}_translate_anchor`, { x: 0, y: 0, extent_x: initial, extent_y: initial })
    .signal(`${ZOOM_PARAM}_translate_delta`, { x: -80, y: 0 })
    .run();
  const after = view.scale("x").domain() as [number, number];
  assert.notDeepEqual(after, initial, "drag must translate the x domain");
  assert.ok(after[0] < initial[0], "leftward drag shifts x domain toward lower values");
  await view.finalize();
});

test("runtime: brush tuple carries channel-tagged extents for plain fields", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const tuples: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => tuples.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: [
      { field: "x", channel: "x", type: "R" },
      { field: "y", channel: "y", type: "R" },
    ],
    values: [[20, 60], [10, 40]],
  }).run();
  assert.equal(tuples.length, 1);
  const domains = tupleDomains(tuples[0], ["x", "y"]);
  assert.deepEqual(domains, { x: [20, 60], y: [10, 40] });
  await view.finalize();
});

test("runtime: brush tuple carries channel-tagged extents for nested `a.b` field paths", async () => {
  // The user-facing `wiki_brush` signal escapes nested paths inconsistently
  // across Vega versions and would break field-name lookup. The tuple keys
  // by channel metadata, which is stable.
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ a: { b: 0, c: 0 } }, { a: { b: 10, c: 10 } }] },
    encoding: {
      x: { field: "a.b", type: "quantitative" },
      y: { field: "a.c", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const tuples: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => tuples.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: [
      { field: "a.b", channel: "x", type: "R" },
      { field: "a.c", channel: "y", type: "R" },
    ],
    values: [[2, 8], [3, 7]],
  }).run();
  const domains = tupleDomains(tuples.at(-1), ["x", "y"]);
  assert.deepEqual(domains, { x: [2, 8], y: [3, 7] });
  await view.finalize();
});

test("runtime: brush tuple resolves array-index `a[0]` field paths", async () => {
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ a: [0, 0] }, { a: [10, 10] }] },
    encoding: {
      x: { field: "a[0]", type: "quantitative" },
      y: { field: "a[1]", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const tuples: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => tuples.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: [
      { field: "a[0]", channel: "x", type: "R" },
      { field: "a[1]", channel: "y", type: "R" },
    ],
    values: [[2, 5], [1, 9]],
  }).run();
  const domains = tupleDomains(tuples.at(-1), ["x", "y"]);
  assert.deepEqual(domains, { x: [2, 5], y: [1, 9] });
  await view.finalize();
});

test("compile: same-field-both-axes aliasing binds BOTH scales and projects BOTH channels", () => {
  // The classic Vega-Lite dedup: one interval parameter with `encodings: [x,y]`
  // and same field on both axes compiles wiki_zoom for x only — y scale gets
  // no domainRaw and wiki_brush_tuple_fields carries only the x entry. The
  // aliasing transform in buildInteractiveSpec must produce a spec whose
  // COMPILED output shows both scales bound and both channels projected.
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;

  const xScale = (compiled.scales ?? []).find((s: Record<string, unknown>) => s.name === "x");
  const yScale = (compiled.scales ?? []).find((s: Record<string, unknown>) => s.name === "y");
  assert.ok((xScale as Record<string, unknown>)?.domainRaw, "x scale must have domainRaw");
  assert.ok((yScale as Record<string, unknown>)?.domainRaw, "y scale must have domainRaw (dedup fix)");

  // Inspect what Vega-Lite ACTUALLY compiled for the brush projection —
  // don't hand-write the tuple; read the compiled tuple_fields default.
  const tupleFields = (compiled.signals ?? []).find(
    (s: Record<string, unknown>) => s.name === `${BRUSH_TUPLE_SIGNAL}_fields`,
  ) as Record<string, unknown>;
  const value = tupleFields?.value as Array<Record<string, unknown>>;
  const channels = value.map((entry) => entry.channel).sort();
  assert.deepEqual(channels, ["x", "y"], "compiled brush must project both channels");
});

test("runtime: same-field-both-axes brush yields two channel extents on the compiler-shaped tuple", async () => {
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;
  const view = new View(parse(compiled), { renderer: "none" as never });
  await view.runAsync();

  // Read the tuple shape Vega generated — same shape a real brush gesture
  // would produce. If aliasing is missing, this default has only one entry
  // and any x/y assumption below breaks.
  const tupleFields = (compiled.signals ?? []).find(
    (s: Record<string, unknown>) => s.name === `${BRUSH_TUPLE_SIGNAL}_fields`,
  ) as Record<string, unknown>;
  const compiledFields = tupleFields.value as Array<Record<string, unknown>>;
  assert.equal(compiledFields.length, 2, "aliasing must produce a two-channel tuple template");

  const captured: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => captured.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: compiledFields,
    values: [[2, 8], [3, 7]],
  }).run();
  const domains = tupleDomains(captured.at(-1), ["x", "y"]);
  assert.deepEqual(domains, { x: [2, 8], y: [3, 7] });
  await view.finalize();
});

test("runtime: same-field a[0] aliasing produces real domains (array-index paths)", async () => {
  // Reviewer regression: a literal datum["a[0]"] in the alias calculate
  // returns undefined, Vega drops every row, and the resulting scale
  // domains are [NaN, NaN]. Correct splitFieldPath yields datum["a"]["0"]
  // and the rows survive.
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ a: [5, 6] }, { a: [7, 8] }, { a: [10, 3] }] },
    encoding: {
      x: { field: "a[0]", type: "quantitative" },
      y: { field: "a[0]", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;
  const view = new View(parse(compiled), { renderer: "none" as never });
  await view.runAsync();
  const rows = view.data("source_0") as unknown[];
  assert.ok(rows.length > 0, "aliased rows must survive the calculate transform");
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]), "x domain must be numeric");
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]), "y domain must be numeric");
  await view.finalize();
});

test("runtime: same-field escaped-dot aliasing (literal `a.b` key) produces real domains", async () => {
  // The field name is the literal key "a.b" on the datum, not nested a→b.
  // Vega-Lite writes that as `a\\.b`. accessExpression must yield
  // datum["a.b"] rather than datum["a"]["b"] (which would be undefined here).
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ "a.b": 5 }, { "a.b": 8 }, { "a.b": 12 }] },
    encoding: {
      x: { field: "a\\.b", type: "quantitative" },
      y: { field: "a\\.b", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;
  const view = new View(parse(compiled), { renderer: "none" as never });
  await view.runAsync();
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]), "escaped-dot x domain must be numeric");
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]), "escaped-dot y domain must be numeric");
  await view.finalize();
});

test("compile: same-field alias preserves axis:null (hidden axes stay hidden)", () => {
  // Reviewer regression: rewrite must not materialize an axis object where
  // the author explicitly set axis:null — that would reveal an axis the
  // author hid, exposing the workaround visually.
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative", axis: null },
    },
  }) as Record<string, unknown>;
  const yEncoding = (interactive.encoding as Record<string, unknown>).y as Record<string, unknown>;
  assert.strictEqual(yEncoding.axis, null, "axis:null must survive the alias rewrite");
  assert.equal(yEncoding.field, "__wiki_plot_y_axis__", "field was still aliased");
});

test("compile: same-field alias preserves an encoding-level custom title", () => {
  // Reviewer regression: an encoding-level `title` must not be overridden by
  // an injected `axis.title` of the raw field name. Precedence: existing
  // axis title > encoding title > raw field name.
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative", title: "Custom Label" },
    },
  }) as Record<string, unknown>;
  const yEncoding = (interactive.encoding as Record<string, unknown>).y as Record<string, unknown>;
  assert.equal(yEncoding.title, "Custom Label", "encoding title must survive");
  // No axis was materialized because the encoding title already provides one.
  assert.equal(yEncoding.axis, undefined, "no axis object added when encoding title exists");
});

test("compile: same-field alias preserves an existing axis.title", () => {
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative", axis: { title: "Y Axis" } },
    },
  }) as Record<string, unknown>;
  const yEncoding = (interactive.encoding as Record<string, unknown>).y as Record<string, unknown>;
  const yAxis = yEncoding.axis as Record<string, unknown>;
  assert.equal(yAxis.title, "Y Axis", "existing axis.title must survive unchanged");
});

test("compile: same-field alias defaults axis.title to the original field name when none is set", () => {
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  }) as Record<string, unknown>;
  const yEncoding = (interactive.encoding as Record<string, unknown>).y as Record<string, unknown>;
  const yAxis = yEncoding.axis as Record<string, unknown>;
  // Original field name shown, not the synthetic alias.
  assert.equal(yAxis.title, "v");
});

test("compile: same-field alias preserves unrelated axis config while adding a title", () => {
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative", axis: { grid: false, labelAngle: 45 } },
    },
  }) as Record<string, unknown>;
  const yEncoding = (interactive.encoding as Record<string, unknown>).y as Record<string, unknown>;
  const yAxis = yEncoding.axis as Record<string, unknown>;
  assert.equal(yAxis.grid, false);
  assert.equal(yAxis.labelAngle, 45);
  assert.equal(yAxis.title, "v", "title added because none was set on axis or encoding");
});

test("compile: composite marks (boxplot/errorbar/errorband) never enter full mode", () => {
  for (const mark of ["boxplot", "errorbar", "errorband"] as const) {
    const spec = {
      mark,
      data: { values: [{ x: 1, y: 1 }] },
      encoding: {
        x: { field: "x", type: "quantitative" },
        y: { field: "y", type: "quantitative" },
      },
    };
    assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" }, `${mark} must degrade to tooltip`);
    // And the resulting tooltip-mode spec still compiles clean — no injected
    // interval selection means no "Selection not supported for X" warnings
    // and no dead Reset button in the UI.
    const interactive = transform(spec);
    const compiled = compile(interactive as never).spec;
    const zoomSignals = (compiled.signals ?? []).filter(
      (s: Record<string, unknown>) => typeof s.name === "string" && (s.name as string).startsWith("wiki_"),
    );
    assert.equal(zoomSignals.length, 0, `${mark} spec must not inject any wiki_zoom/wiki_brush signals`);
  }
});

test("parse: existing wiki_zoom exact-name collision degrades and parses clean", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: ZOOM_PARAM, select: { type: "interval" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => parse(compile(interactive as never).spec));
});

test("parse: existing wiki_zoom_x derived-name collision degrades and parses clean", () => {
  // Vega-Lite compiles a per-channel signal named wiki_zoom_x from our
  // injected wiki_zoom param. A user param with the same name would collide
  // at vega.parse (NOT at vega-lite compile) with "Duplicate signal name".
  // The classifier must reject this at inject time.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${ZOOM_PARAM}_x`, value: 5 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(
    () => parse(compile(interactive as never).spec),
    "vega.parse must not throw Duplicate signal name",
  );
});

test("parse: existing wiki_brush_tuple derived-name collision degrades and parses clean", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: BRUSH_TUPLE_SIGNAL, value: null }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => parse(compile(interactive as never).spec));
});

test("parse: leaving the collision-guard off would in fact throw at parse (guard regression)", () => {
  // Explicit demonstration: bypass the classifier and inject the full-mode
  // params on a spec that already reserves wiki_zoom_x. Verifies the guard
  // actually protects against a real runtime failure — if this ever stops
  // throwing, the guard's justification has gone away.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${ZOOM_PARAM}_x`, value: 5 }],
  };
  const forced = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
  }) as Record<string, unknown>;
  assert.throws(
    () => parse(compile(forced as never).spec),
    /Duplicate signal name/,
    "sanity: guard bypass reproduces the runtime failure the guard prevents",
  );
});
