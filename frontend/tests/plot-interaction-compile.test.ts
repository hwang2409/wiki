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

test("runtime: same field on both axes yields two distinct channel extents", async () => {
  // The classic bug: the user-facing `wiki_brush` signal returns `{v: [x-extent]}`,
  // collapsing y-extent into x-extent. The tuple preserves both because the
  // channel tag is on the metadata entry, not on the map key.
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
  const view = await renderHeadless(interactive);
  const tuples: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => tuples.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: [
      { field: "v", channel: "x", type: "R" },
      { field: "v", channel: "y", type: "R" },
    ],
    values: [[2, 8], [3, 7]],
  }).run();
  const domains = tupleDomains(tuples.at(-1), ["x", "y"]);
  assert.deepEqual(domains, { x: [2, 8], y: [3, 7] });
  await view.finalize();
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
