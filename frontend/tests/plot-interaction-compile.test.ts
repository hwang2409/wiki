// Compile-level and vega-runtime assertions for WIKI-194. Push the
// transformed spec through the actual vega-lite compiler AND the vega
// runtime so any wiring regression — bind:scales in wrong place, name
// collision, same-field dedup, user-selection binding theft, wrong brush
// visual — fails immediately, not silently in the browser.
import assert from "node:assert/strict";
import test from "node:test";
import { compile } from "vega-lite";
import { View, parse } from "vega";
import {
  BRUSH_PARAM,
  BRUSH_TUPLE_SIGNAL,
  ZOOM_PARAM_PREFIX,
  buildInteractiveSpec,
  plotInteractivity,
  tupleDomains,
  zoomParamName,
} from "../src/plot-interaction.ts";

function transform(spec: Record<string, unknown>): Record<string, unknown> {
  const interactivity = plotInteractivity(spec);
  return buildInteractiveSpec(spec, {
    interactivity,
    armed: true,
    brushColor: "#333333",
  }) as Record<string, unknown>;
}

async function renderHeadless(spec: Record<string, unknown>): Promise<View> {
  const compiled = compile(spec as never).spec;
  const runtime = parse(compiled);
  const view = new View(runtime, { renderer: "none" as never });
  await view.runAsync();
  return view;
}

const CONTINUOUS: Record<string, unknown> = {
  mark: "point",
  data: { values: [{ x: 1, y: 1 }, { x: 4, y: 3 }] },
  encoding: {
    x: { field: "x", type: "quantitative" },
    y: { field: "y", type: "quantitative" },
  },
};

test("compile: per-channel zoom params bind their own scales via domainRaw", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const scales = (output.spec.scales ?? []) as Array<Record<string, unknown>>;
  for (const channel of ["x", "y"] as const) {
    const scale = scales.find((s) => s.name === channel)!;
    const domainRaw = scale.domainRaw as Record<string, unknown> | undefined;
    assert.ok(domainRaw, `${channel} scale must have domainRaw`);
    assert.equal(
      domainRaw.signal,
      `${zoomParamName(channel)}["${channel}"]`,
      `${channel} domainRaw must reference ${zoomParamName(channel)}`,
    );
  }
});

test("compile: per-channel zoom signals carry panLinear and zoomLinear updates", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const signals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  for (const channel of ["x", "y"] as const) {
    const zoom = signals.find((s) => s.name === `${zoomParamName(channel)}_${channel}`)!;
    const zoomOn = JSON.stringify(zoom.on ?? []);
    assert.match(zoomOn, /panLinear\(/);
    assert.match(zoomOn, new RegExp(`zoomLinear\\(domain\\(\\\\?"${channel}\\\\?"\\)`));
  }
});

test("compile: mixed domainRaw axis keeps its binding while the other axis gets zoom", () => {
  const spec = {
    ...CONTINUOUS,
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domainRaw: { signal: "[2, 8]" } } },
      y: { field: "y", type: "quantitative" },
    },
  };
  const interactive = transform(spec);
  const output = compile(interactive as never);
  const scales = (output.spec.scales ?? []) as Array<Record<string, unknown>>;
  const xScale = scales.find((scale) => scale.name === "x")!;
  const yScale = scales.find((scale) => scale.name === "y")!;
  assert.deepEqual(xScale.domainRaw, { signal: "[2, 8]" });
  assert.equal(
    (yScale.domainRaw as Record<string, unknown>).signal,
    `${zoomParamName("y")}["y"]`,
  );
  const injectedSignals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  assert.equal(injectedSignals.some((signal) => signal.name === `${zoomParamName("x")}_x`), false);
  assert.equal(injectedSignals.some((signal) => signal.name === `${zoomParamName("y")}_y`), true);
});

test("compile: all domainRaw axes inject no plot controls", () => {
  const spec = {
    ...CONTINUOUS,
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domainRaw: { signal: "[2, 8]" } } },
      y: { field: "y", type: "quantitative", scale: { domainRaw: { signal: "[1, 9]" } } },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const compiled = compile(transform(spec) as never).spec;
  const injected = (compiled.signals ?? []).filter(
    (signal: Record<string, unknown>) => typeof signal.name === "string" && signal.name.startsWith("wiki_"),
  );
  assert.equal(injected.length, 0);
});

test("compile: single visible 2D brush for distinct-field x+y — mark spans BOTH bounds", () => {
  // R8F2: two 1D brushes rendered a cross while the applied zoom was the
  // intersection box. One 2D brush must both compile AND draw a rectangle
  // whose bounds come from BOTH wiki_brush_x and wiki_brush_y signals.
  const interactive = transform(CONTINUOUS);
  const compiled = compile(interactive as never).spec;
  const brushMark = (compiled.marks ?? []).find(
    (m: Record<string, unknown>) => m.name === `${BRUSH_PARAM}_brush_bg`,
  ) as Record<string, unknown>;
  const update = (brushMark.encode as Record<string, unknown>).update as Record<string, unknown>;
  const bounds = ["x", "y", "x2", "y2"] as const;
  for (const key of bounds) {
    const entries = update[key] as Array<Record<string, unknown>>;
    const activeEntry = entries[0];
    // Both x/x2 must reference a wiki_brush_x* signal, both y/y2 must
    // reference a wiki_brush_y* signal — proof that shift-drag paints a
    // box, not a cross. Vega-Lite picks between wiki_brush_x (data-space)
    // and wiki_brush_x_1 (pixel-space) depending on whether the brush is
    // scale-bound; both are valid signal-driven bounds and either satisfies
    // "box, not cross".
    const signal = String((activeEntry as { signal?: string }).signal ?? "");
    const prefix = key.startsWith("x") ? `${BRUSH_PARAM}_x` : `${BRUSH_PARAM}_y`;
    assert.match(signal, new RegExp(`^${prefix}(_\\d+)?\\[[01]\\]$`),
      `brush ${key} bound must come from a ${prefix}* signal (got ${signal || "no signal"})`);
  }
});

test("compile: same-field brush projects x only — visible band matches applied x-zoom", () => {
  // For same-field, the compiled brush mark spans full-height because y is
  // dropped from the projection. The visible x-band then matches what
  // actually happens on release (only x zooms).
  const interactive = transform({
    mark: "point",
    data: { values: [{ v: 0 }, { v: 10 }] },
    encoding: {
      x: { field: "v", type: "quantitative" },
      y: { field: "v", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;
  const brushMark = (compiled.marks ?? []).find(
    (m: Record<string, unknown>) => m.name === `${BRUSH_PARAM}_brush_bg`,
  ) as Record<string, unknown>;
  const update = (brushMark.encode as Record<string, unknown>).update as Record<string, unknown>;
  // x bound comes from wiki_brush_x (real x-selection)
  const xEntry = (update.x as Array<Record<string, unknown>>)[0];
  assert.equal(xEntry.signal, `${BRUSH_PARAM}_x[0]`);
  // y bound is a static value or group height — NOT a signal — because we
  // deliberately did not project y. That is what makes the visible band
  // full-height, matching the "x-only zoom on release" outcome.
  const y2Entry = (update.y2 as Array<Record<string, unknown>>)[0];
  assert.notStrictEqual((y2Entry as { signal?: unknown }).signal, `${BRUSH_PARAM}_y[1]`);
});

test("runtime: wheel on wiki_zoom_x shrinks x domain independently of y", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400, height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
  });
  const view = await renderHeadless(interactive);
  const initialX = view.scale("x").domain() as [number, number];
  const initialY = view.scale("y").domain() as [number, number];
  view
    .signal(`${zoomParamName("x")}_zoom_anchor`, { x: 5, y: 5 })
    .signal(`${zoomParamName("x")}_zoom_delta`, 2)
    .run();
  const afterX = view.scale("x").domain() as [number, number];
  const afterY = view.scale("y").domain() as [number, number];
  assert.notDeepEqual(afterX, initialX);
  assert.deepEqual(afterY, initialY, "wheel on x must NOT touch y");
  await view.finalize();
});

test("runtime: keyboard zoom and pan preserve log bounds", async () => {
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ x: 1, y: 1 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: { type: "log" } },
      y: { field: "y", type: "quantitative" },
    },
  });
  const compiled = compile(interactive as never).spec;
  const xSignal = (compiled.signals ?? []).find(
    (signal: Record<string, unknown>) => signal.name === `${zoomParamName("x")}_x`,
  ) as Record<string, unknown>;
  assert.match(JSON.stringify(xSignal.on ?? []), /panLog\(/);
  assert.match(JSON.stringify(xSignal.on ?? []), /zoomLog\(/);

  const view = await renderHeadless(interactive);
  const scale = view.scale("x");
  const range = scale.range();
  const anchor = Number(scale.invert((Number(range[0]) + Number(range[1])) / 2));
  view
    .signal(`${zoomParamName("x")}_zoom_anchor`, { x: anchor, y: anchor })
    .signal(`${zoomParamName("x")}_zoom_delta`, 0.8)
    .run();
  const zoomed = scale.domain() as [number, number];
  assert.ok(zoomed.every((value) => Number.isFinite(value) && value > 0));

  view
    .signal(`${zoomParamName("x")}_translate_anchor`, {
      x: 0,
      y: 0,
      extent_x: zoomed,
      extent_y: zoomed,
    })
    .signal(`${zoomParamName("x")}_translate_delta`, { x: -80, y: 0 })
    .run();
  const panned = scale.domain() as [number, number];
  assert.ok(panned.every((value) => Number.isFinite(value) && value > 0));
  assert.ok(panned[0] < panned[1]);
  await view.finalize();
});

test("runtime: keyboard zoom and pan preserve descending domain order", async () => {
  const interactive = transform({
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ x: 1, y: 1 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domain: [10, 1] } },
      y: { field: "y", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const scale = view.scale("x");
  const initial = scale.domain() as [number, number];
  assert.ok(initial[0] > initial[1]);
  const range = scale.range();
  const anchor = Number(scale.invert((Number(range[0]) + Number(range[1])) / 2));
  view
    .signal(`${zoomParamName("x")}_zoom_anchor`, { x: anchor, y: anchor })
    .signal(`${zoomParamName("x")}_zoom_delta`, 0.8)
    .run();
  const zoomed = scale.domain() as [number, number];
  assert.ok(zoomed.every(Number.isFinite));
  assert.ok(zoomed[0] > zoomed[1]);

  view
    .signal(`${zoomParamName("x")}_translate_anchor`, {
      x: 0,
      y: 0,
      extent_x: zoomed,
      extent_y: zoomed,
    })
    .signal(`${zoomParamName("x")}_translate_delta`, { x: -80, y: 0 })
    .run();
  const panned = scale.domain() as [number, number];
  assert.ok(panned.every(Number.isFinite));
  assert.ok(panned[0] > panned[1]);
  await view.finalize();
});

test("runtime: mixed domainRaw axis stays fixed while the other axis zooms", async () => {
  const spec = {
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domainRaw: { signal: "[2, 8]" } } },
      y: { field: "y", type: "quantitative" },
    },
  };
  const interactive = transform(spec);
  assert.deepEqual(plotInteractivity(spec), { mode: "full", channels: ["y"] });
  const view = await renderHeadless(interactive);
  const initialX = view.scale("x").domain() as [number, number];
  const initialY = view.scale("y").domain() as [number, number];
  view
    .signal(`${zoomParamName("y")}_zoom_anchor`, { x: 5, y: 5 })
    .signal(`${zoomParamName("y")}_zoom_delta`, 2)
    .run();
  assert.deepEqual(view.scale("x").domain(), initialX);
  assert.notDeepEqual(view.scale("y").domain(), initialY);
  await view.finalize();
});

test("runtime: descending brush domain stays descending after re-embed", async () => {
  const spec = {
    mark: "point",
    width: 400,
    height: 200,
    data: { values: [{ x: 1, y: 1 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative", scale: { domain: [10, 1] } },
      y: { field: "y", type: "quantitative" },
    },
  };
  const initialView = await renderHeadless(transform(spec));
  const brushDomains = tupleDomains(
    {
      fields: [{ field: "x", channel: "x", type: "R" }],
      values: [[1, 10]],
    },
    ["x"],
    { x: "descending" },
  );
  assert.deepEqual(brushDomains, { x: [10, 1] });
  await initialView.finalize();

  const reboundSpec = {
    ...spec,
    encoding: {
      ...spec.encoding,
      x: { ...spec.encoding.x, scale: { domain: brushDomains!.x } },
    },
  };
  const reboundView = await renderHeadless(transform(reboundSpec));
  assert.deepEqual(reboundView.scale("x").domain(), [10, 1]);
  await reboundView.finalize();
});

test("runtime: leftward drag shifts x domain toward lower values", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400, height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const initial = view.scale("x").domain() as [number, number];
  view
    .signal(`${zoomParamName("x")}_translate_anchor`, { x: 0, y: 0, extent_x: initial })
    .signal(`${zoomParamName("x")}_translate_delta`, { x: -80, y: 0 })
    .run();
  const after = view.scale("x").domain() as [number, number];
  assert.ok(after[0] < initial[0]);
  await view.finalize();
});

test("runtime: single 2D brush yields channel-tagged extents for both axes", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400, height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const captured: unknown[] = [];
  view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_n, value) => captured.push(value));
  view.signal(BRUSH_TUPLE_SIGNAL, {
    unit: "",
    fields: [
      { field: "x", channel: "x", type: "R" },
      { field: "y", channel: "y", type: "R" },
    ],
    values: [[20, 60], [10, 40]],
  }).run();
  const domains = tupleDomains(captured.at(-1), ["x", "y"]);
  assert.deepEqual(domains, { x: [20, 60], y: [10, 40] });
  await view.finalize();
});

test("runtime: same-field a[0] path stays interactive (no data transform injected)", async () => {
  const interactive = transform({
    mark: "point",
    width: 400, height: 200,
    data: { values: [{ a: [5, 6] }, { a: [7, 8] }, { a: [10, 3] }] },
    encoding: {
      x: { field: "a[0]", type: "quantitative" },
      y: { field: "a[0]", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]));
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]));
  assert.equal(interactive.transform, undefined, "no data transform injected");
  await view.finalize();
});

test("runtime: escaped-dot field path stays interactive", async () => {
  const interactive = transform({
    mark: "point",
    width: 400, height: 200,
    data: { values: [{ "a.b": 5 }, { "a.b": 8 }, { "a.b": 12 }] },
    encoding: {
      x: { field: "a\\.b", type: "quantitative" },
      y: { field: "a\\.b", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]));
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]));
  await view.finalize();
});

test("runtime: nested `a.b` field path stays interactive", async () => {
  const interactive = transform({
    mark: "point",
    width: 400, height: 200,
    data: { values: [{ a: { b: 5 } }, { a: { b: 8 } }, { a: { b: 12 } }] },
    encoding: {
      x: { field: "a.b", type: "quantitative" },
      y: { field: "a.b", type: "quantitative" },
    },
  });
  const view = await renderHeadless(interactive);
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]));
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]));
  await view.finalize();
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
  assert.equal(plotInteractivity(spec).mode, "full");
  const interactive = transform(spec);
  assert.doesNotThrow(() => compile(interactive as never));
});

test("compile: composite marks never enter full mode and inject no wiki_* signals", () => {
  for (const mark of ["boxplot", "errorbar", "errorband"] as const) {
    const spec = {
      mark,
      data: { values: [{ x: 1, y: 1 }] },
      encoding: {
        x: { field: "x", type: "quantitative" },
        y: { field: "y", type: "quantitative" },
      },
    };
    assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
    const interactive = transform(spec);
    const compiled = compile(interactive as never).spec;
    const injected = (compiled.signals ?? []).filter(
      (s: Record<string, unknown>) => typeof s.name === "string" && (s.name as string).startsWith("wiki_"),
    );
    assert.equal(injected.length, 0, `${mark} must not inject wiki_* signals`);
  }
});

test("runtime: existing user bind:scales interval — guard preserves the user's binding", async () => {
  // R8F1 regression: without downgrade, our wiki_zoom_x binding replaces
  // the user's `user_pan` binding on the x scale. Verify that when the guard
  // downgrades (no wiki_* injection), the user's interval still binds the
  // scale — i.e. we haven't broken their interaction.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
    width: 400, height: 200,
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: "user_pan", select: { type: "interval" }, bind: "scales" }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" }, "guard must downgrade");
  const interactive = transform(spec);
  const compiled = compile(interactive as never).spec;
  const xScale = (compiled.scales ?? []).find((s: Record<string, unknown>) => s.name === "x") as Record<string, unknown>;
  const domainRaw = xScale.domainRaw as Record<string, unknown>;
  // The user's interval STILL binds the x scale — we didn't steal it because
  // we didn't inject wiki_zoom_x.
  assert.equal(domainRaw.signal, `user_pan["x"]`, "user_pan retains x scale binding");
});

test("compile: guard-bypass control — force full inject alongside user bind:scales STEALS x binding", () => {
  // Sanity for R8F1: if buildInteractiveSpec is forced to inject alongside a
  // user bind:scales interval, the compiled x scale binds wiki_zoom_x, not
  // user_pan — this IS the silent break the guard prevents.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: "user_pan", select: { type: "interval" }, bind: "scales" }],
  };
  const forced = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
  }) as Record<string, unknown>;
  const compiled = compile(forced as never).spec;
  const xScale = (compiled.scales ?? []).find((s: Record<string, unknown>) => s.name === "x") as Record<string, unknown>;
  const domainRaw = xScale.domainRaw as Record<string, unknown>;
  assert.equal(domainRaw.signal, `${zoomParamName("x")}["x"]`,
    "without downgrade, wiki_zoom_x steals the binding from user_pan");
});

test("parse: exact wiki_zoom name collision degrades and parses clean", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: ZOOM_PARAM_PREFIX, value: 1 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => parse(compile(interactive as never).spec));
});

test("parse: derived wiki_zoom_x_x collision (bypass) throws Duplicate signal name", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${zoomParamName("x")}_x`, value: 5 }],
  };
  const forced = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
  }) as Record<string, unknown>;
  assert.throws(() => parse(compile(forced as never).spec), /Duplicate signal name/);
});

test("runtime: user dataset named wiki_brush_store — guard prevents source replacement", async () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { name: "wiki_brush_store", values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  const view = await renderHeadless(interactive);
  const rows = view.data("wiki_brush_store") as unknown[];
  assert.equal(rows.length, 2, "user dataset must remain intact");
  await view.finalize();
});

test("compile: legacy top-level `selection` spec — guard prevents dead inject", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    selection: { legacy_pan: { type: "interval", bind: "scales" } },
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  const compiled = compile(interactive as never).spec;
  const wikiSignals = (compiled.signals ?? []).filter(
    (s: Record<string, unknown>) => typeof s.name === "string" && (s.name as string).startsWith("wiki_"),
  );
  assert.equal(wikiSignals.length, 0);
});
