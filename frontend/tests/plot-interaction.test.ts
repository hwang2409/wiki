import assert from "node:assert/strict";
import test from "node:test";
import {
  BRUSH_PARAM,
  ZOOM_PARAM,
  buildInteractiveSpec,
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
  assert.equal((zoom.select as Record<string, unknown>).bind, "scales");
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

test("plotPngFilename: sanitizes and defaults", () => {
  assert.equal(plotPngFilename(null), "plot.png");
  assert.equal(plotPngFilename(""), "plot.png");
  assert.equal(plotPngFilename("  "), "plot.png");
  assert.equal(plotPngFilename("Revenue / 2025"), "Revenue-2025.png");
  assert.equal(plotPngFilename("---leading"), "leading.png");
  assert.equal(plotPngFilename("chart.v1"), "chart.v1.png");
});
