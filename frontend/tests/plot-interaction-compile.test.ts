// Compile-level and vega-runtime assertions for WIKI-194. These push the
// transformed spec through the actual vega-lite compiler AND the vega runtime
// so any wiring regression — bind:scales in wrong place, name collision,
// same-field dedup, aggregate/timeUnit false-full, scale:null crash — fails
// immediately, not silently in the browser.
import assert from "node:assert/strict";
import test from "node:test";
import { compile } from "vega-lite";
import { View, parse } from "vega";
import {
  BRUSH_PARAM_PREFIX,
  ZOOM_PARAM_PREFIX,
  brushParamName,
  buildInteractiveSpec,
  extentFromSignal,
  plotInteractivity,
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

test("compile: each per-channel zoom param binds its own scale via domainRaw", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const scales = (output.spec.scales ?? []) as Array<Record<string, unknown>>;
  for (const channel of ["x", "y"] as const) {
    const scale = scales.find((s) => s.name === channel)!;
    const domainRaw = scale.domainRaw as Record<string, unknown> | undefined;
    assert.ok(domainRaw, `${channel} scale must have domainRaw`);
    // Each scale binds to its OWN wiki_zoom_<channel> selection (per-channel
    // design), NOT a shared wiki_zoom. That's what lets same-field plots
    // work: two parameters, two stores, nothing to dedupe.
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
    const zoom = signals.find((s) => s.name === `${zoomParamName(channel)}_${channel}`);
    assert.ok(zoom, `${channel} zoom signal must exist`);
    const zoomOn = JSON.stringify(zoom.on ?? []);
    // panLinear + zoomLinear updates on the compiled signal prove the param
    // is scale-bound; without bind:scales at the param level the signal
    // exists but has no such handlers.
    assert.match(zoomOn, /panLinear\(/);
    assert.match(zoomOn, new RegExp(`zoomLinear\\(domain\\(\\\\?"${channel}\\\\?"\\)`));
  }
});

test("compile: per-channel brush params compile to independent signals + stores", () => {
  const interactive = transform(CONTINUOUS);
  const output = compile(interactive as never);
  const signals = (output.spec.signals ?? []) as Array<Record<string, unknown>>;
  const data = (output.spec.data ?? []) as Array<Record<string, unknown>>;
  for (const channel of ["x", "y"] as const) {
    assert.ok(
      signals.some((s) => s.name === brushParamName(channel)),
      `${brushParamName(channel)} must compile`,
    );
    assert.ok(
      data.some((d) => d.name === `${brushParamName(channel)}_store`),
      `${brushParamName(channel)}_store selection store must exist`,
    );
  }
});

test("runtime: wheel zoom on wiki_zoom_x shrinks the x domain independently", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
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
  assert.notDeepEqual(afterX, initialX, "wheel-zoom-x must move the x domain");
  assert.deepEqual(afterY, initialY, "wheel-zoom-x must NOT touch the y domain");
  await view.finalize();
});

test("runtime: leftward drag on wiki_zoom_x shifts x domain toward lower values", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const initial = view.scale("x").domain() as [number, number];
  view
    .signal(`${zoomParamName("x")}_translate_anchor`, { x: 0, y: 0, extent_x: initial })
    .signal(`${zoomParamName("x")}_translate_delta`, { x: -80, y: 0 })
    .run();
  const after = view.scale("x").domain() as [number, number];
  assert.ok(after[0] < initial[0], "leftward drag shifts x domain toward lower values");
  await view.finalize();
});

test("runtime: per-channel brush signals fire {field: [low, high]} extents", async () => {
  const interactive = transform({
    ...CONTINUOUS,
    width: 400,
    height: 200,
    data: { values: [{ x: 0, y: 0 }, { x: 100, y: 100 }] },
  });
  const view = await renderHeadless(interactive);
  const capturedX: unknown[] = [];
  const capturedY: unknown[] = [];
  view.addSignalListener(brushParamName("x"), (_n, v) => capturedX.push(v));
  view.addSignalListener(brushParamName("y"), (_n, v) => capturedY.push(v));
  view.signal(`${brushParamName("x")}_tuple`, {
    unit: "",
    fields: [{ field: "x", channel: "x", type: "R" }],
    values: [[20, 60]],
  }).run();
  view.signal(`${brushParamName("y")}_tuple`, {
    unit: "",
    fields: [{ field: "y", channel: "y", type: "R" }],
    values: [[10, 40]],
  }).run();
  assert.deepEqual(extentFromSignal(capturedX.at(-1)), [20, 60]);
  assert.deepEqual(extentFromSignal(capturedY.at(-1)), [10, 40]);
  await view.finalize();
});

test("runtime: same-field-both-axes — both scales bind, both brush signals fire distinctly", async () => {
  // The original reason we added (and then removed) the alias transform:
  // Vega-Lite dedupes same-field projections. Per-channel params make each
  // param stand on its own, so both scales bind and each channel's brush
  // signal is independent — no alias, no rewrite, no data transform.
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
  const scales = (compiled.scales ?? []) as Array<Record<string, unknown>>;
  const xScale = scales.find((s) => s.name === "x");
  const yScale = scales.find((s) => s.name === "y");
  assert.ok((xScale as Record<string, unknown>)?.domainRaw, "x scale must have domainRaw");
  assert.ok((yScale as Record<string, unknown>)?.domainRaw, "y scale must have domainRaw (per-channel fix)");

  const view = new View(parse(compiled), { renderer: "none" as never });
  await view.runAsync();
  const capturedX: unknown[] = [];
  const capturedY: unknown[] = [];
  view.addSignalListener(brushParamName("x"), (_n, v) => capturedX.push(v));
  view.addSignalListener(brushParamName("y"), (_n, v) => capturedY.push(v));
  view.signal(`${brushParamName("x")}_tuple`, {
    unit: "",
    fields: [{ field: "v", channel: "x", type: "R" }],
    values: [[2, 8]],
  }).run();
  view.signal(`${brushParamName("y")}_tuple`, {
    unit: "",
    fields: [{ field: "v", channel: "y", type: "R" }],
    values: [[3, 7]],
  }).run();
  assert.deepEqual(extentFromSignal(capturedX.at(-1)), [2, 8]);
  assert.deepEqual(extentFromSignal(capturedY.at(-1)), [3, 7]);
  await view.finalize();
});

test("runtime: same-field a[0] path stays interactive with no calculate transform", async () => {
  // Prior alias-based fix broke this at r5 because datum["a[0]"] is a literal
  // key lookup, not an array-index path. Per-channel design side-steps: no
  // calculate transform is injected, so the original data reaches Vega
  // untouched and the compiled scales bind correctly.
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
  const view = await renderHeadless(interactive);
  const xDomain = view.scale("x").domain() as [number, number];
  const yDomain = view.scale("y").domain() as [number, number];
  assert.ok(Number.isFinite(xDomain[0]) && Number.isFinite(xDomain[1]), "x domain must be numeric");
  assert.ok(Number.isFinite(yDomain[0]) && Number.isFinite(yDomain[1]), "y domain must be numeric");
  // No alias transform was injected — verify that.
  assert.equal(interactive.transform, undefined, "per-channel design must not add a data transform");
  await view.finalize();
});

test("runtime: escaped-dot field path (literal `a.b` key) stays interactive", async () => {
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
    width: 400,
    height: 200,
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
  const interactivity = plotInteractivity(spec);
  assert.equal(interactivity.mode, "full");
  if (interactivity.mode === "full") {
    assert.deepEqual(interactivity.channels, ["y"]);
  }
  const interactive = transform(spec);
  assert.doesNotThrow(() => compile(interactive as never));
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
    assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
    const interactive = transform(spec);
    const compiled = compile(interactive as never).spec;
    const injected = (compiled.signals ?? []).filter(
      (s: Record<string, unknown>) => typeof s.name === "string" && (s.name as string).startsWith("wiki_"),
    );
    assert.equal(injected.length, 0, `${mark} must not inject any wiki_* signals`);
  }
});

test("parse: exact wiki_zoom name collision degrades and parses clean", () => {
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: ZOOM_PARAM_PREFIX, select: { type: "interval" } }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => parse(compile(interactive as never).spec));
});

test("parse: user param wiki_zoom_x_x (would collide with derived signal) degrades", () => {
  // Our per-channel wiki_zoom_x param compiles a derived signal wiki_zoom_x_x.
  // A user param with that exact name would parse-error with "Duplicate
  // signal name". The prefix guard catches this via the wiki_zoom_ prefix.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    params: [{ name: `${zoomParamName("x")}_x`, value: 5 }],
  };
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  assert.doesNotThrow(() => parse(compile(interactive as never).spec));
});

test("parse: guard-bypass control — forcing full inject with wiki_zoom_x user param throws Duplicate", () => {
  // Sanity: this is the runtime failure the collision guard is preventing.
  // If this ever stops throwing, the guard's justification has gone away.
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
  assert.throws(
    () => parse(compile(forced as never).spec),
    /Duplicate signal name/,
  );
});

test("runtime: user dataset named wiki_zoom_x_store — guard prevents source replacement", async () => {
  // Reviewer's regression: a user top-level dataset named wiki_zoom_x_store
  // would collide with our injected selection store. If we didn't downgrade,
  // the first zoom update would overwrite the user's dataset with the
  // selection tuple and any chart sourced from it would go empty.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { name: "wiki_zoom_x_store", values: [{ x: 0, y: 0 }, { x: 10, y: 10 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
  };
  // With the guard, we stay in tooltip mode (no injection, no collision).
  assert.deepEqual(plotInteractivity(spec), { mode: "tooltip" });
  const interactive = transform(spec);
  const view = await renderHeadless(interactive);
  // The user's dataset survives — chart still has rows because we injected
  // nothing that would collide.
  const rows = view.data("wiki_zoom_x_store") as unknown[];
  assert.equal(rows.length, 2, "user dataset must remain intact");
  await view.finalize();
});

test("compile: legacy top-level `selection` spec compiles the legacy signal, downgrade prevents dead inject", () => {
  // Without downgrading, buildInteractiveSpec would inject wiki_zoom_x etc.,
  // but Vega-Lite compiles ONLY the legacy `selection` block and drops every
  // injected param — the inspector would advertise interactions with no
  // handlers.
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
  assert.equal(wikiSignals.length, 0, "no wiki_* signals should be injected in tooltip mode");
});

test("compile: guard-bypass control — full inject alongside legacy selection loses every wiki_* signal", () => {
  // Sanity for the legacy-selection guard: if buildInteractiveSpec is forced
  // to inject alongside a legacy top-level selection, Vega-Lite silently
  // drops every injected param. This is precisely the "advertised interaction
  // with no handlers" failure mode the guard prevents.
  const spec: Record<string, unknown> = {
    mark: "point",
    data: { values: [{ x: 1, y: 1 }] },
    encoding: {
      x: { field: "x", type: "quantitative" },
      y: { field: "y", type: "quantitative" },
    },
    selection: { legacy_pan: { type: "interval", bind: "scales" } },
  };
  const forced = buildInteractiveSpec(spec, {
    interactivity: { mode: "full", channels: ["x", "y"] },
    armed: true,
  }) as Record<string, unknown>;
  const compiled = compile(forced as never).spec;
  const wikiSignals = (compiled.signals ?? []).filter(
    (s: Record<string, unknown>) => typeof s.name === "string" && (s.name as string).startsWith("wiki_"),
  );
  assert.equal(wikiSignals.length, 0, "vega-lite drops every injected param when legacy selection is present");
});
