// Compile-level assertions for WIKI-194. These push the transformed spec
// through the actual vega-lite compiler so a subtle mistake (e.g. bind:scales
// nested inside select) can't hide behind a shape-only unit test.
import assert from "node:assert/strict";
import test from "node:test";
import { compile } from "vega-lite";
import { View, parse } from "vega";
import {
  BRUSH_PARAM,
  ZOOM_PARAM,
  buildInteractiveSpec,
  plotInteractivity,
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
  // panLinear / zoomLinear updates on the compiled signal prove the parameter
  // is bound to scales; without bind:scales at the parameter level, the
  // signal exists but has no such update handlers.
  assert.match(zoomOn, /panLinear\(/);
  assert.match(zoomOn, /zoomLinear\(domain\(\\?"x\\?"\)/);
});

test("compile: brush selection compiles the store separately from the zoom param", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const signals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  assert.ok(signals.some((s) => s.name === BRUSH_PARAM), "brush signal must compile");
  assert.ok(signals.some((s) => s.name === ZOOM_PARAM), "zoom signal must compile");
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
  // Without dropping the scale:null channel from the interval `encodings`,
  // Vega-Lite throws "Cannot read properties of undefined (reading get)".
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
  // Fire the wheel-zoom signals Vega listens for on the compiled param.
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
  // Vega's panLinear applies -delta.x/width to the extent: a leftward drag
  // (negative delta.x) shifts the visible domain downward, revealing earlier
  // x values. This proves the compiled param actually moves the scale, not
  // just a selection rectangle.
  assert.ok(after[0] < initial[0], "leftward drag shifts x domain toward lower values");
  await view.finalize();
});

test("runtime: brush signal exposes the field-keyed extent React reads", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const captured: unknown[] = [];
  view.addSignalListener(BRUSH_PARAM, (_name, value) => captured.push(value));
  // The brush signal populates a { fieldName: [low, high] } shape once its
  // interval-selection tuple resolves. Fire the interval store manually with
  // a fully-formed tuple matching what Vega's brush stream produces.
  view.signal(`${BRUSH_PARAM}_tuple`, {
    unit: "",
    fields: [
      { field: "x", channel: "x", type: "R" },
      { field: "y", channel: "y", type: "R" },
    ],
    values: [[20, 60], [10, 40]],
  }).run();
  assert.ok(captured.length > 0, "brush signal must fire on tuple update");
  const last = captured.at(-1) as Record<string, unknown>;
  assert.deepEqual(last.x, [20, 60]);
  assert.deepEqual(last.y, [10, 40]);
  await view.finalize();
});

test("compile: existing wiki_zoom name collision no longer explodes the compile", () => {
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
  assert.doesNotThrow(() => compile(interactive as never));
});
