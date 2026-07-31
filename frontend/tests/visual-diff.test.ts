import assert from "node:assert/strict";
import test from "node:test";
import {
  blendOpacity,
  boundedDiffDimensions,
  computePixelDiff,
  formatDiffPercent,
  MAX_DIFF_PIXELS,
  visualDiffAspect,
  visualDiffSources,
} from "../src/visual-diff.ts";
import type { SessionArtifact, SessionEvent } from "../src/api.ts";

function pixelBuffer(pixels: [number, number, number, number][]): Uint8ClampedArray {
  const buffer = new Uint8ClampedArray(pixels.length * 4);
  pixels.forEach((pixel, index) => {
    buffer[index * 4] = pixel[0];
    buffer[index * 4 + 1] = pixel[1];
    buffer[index * 4 + 2] = pixel[2];
    buffer[index * 4 + 3] = pixel[3];
  });
  return buffer;
}

test("blendOpacity clamps to 0..1 and rejects non-finite values", () => {
  assert.equal(blendOpacity(0), 0);
  assert.equal(blendOpacity(1), 1);
  assert.equal(blendOpacity(0.42), 0.42);
  assert.equal(blendOpacity(-0.5), 0);
  assert.equal(blendOpacity(1.5), 1);
  assert.equal(blendOpacity(Number.NaN), 0);
  assert.equal(blendOpacity(Number.POSITIVE_INFINITY), 0);
});

test("visualDiffSources encodes ticket and artifact id and adds variant", () => {
  const event = { artifact_id: "abc/def", kind: "artifact" } as unknown as SessionEvent;
  const sources = visualDiffSources("WIKI 193", event);
  assert.equal(
    sources.before,
    "/api/agents/WIKI%20193/artifact/abc%2Fdef?variant=before",
  );
  assert.equal(
    sources.after,
    "/api/agents/WIKI%20193/artifact/abc%2Fdef?variant=after",
  );
});

test("visualDiffAspect prefers before dimensions and returns null when missing", () => {
  const withBoth = {
    kind: "visual-diff",
    before: { mime: "image/png", width: 200, height: 100 },
    after: { mime: "image/png", width: 200, height: 100 },
  } as unknown as SessionArtifact;
  assert.equal(visualDiffAspect(withBoth), 2);
  const withoutDims = {
    kind: "visual-diff",
    before: { mime: "image/png" },
    after: { mime: "image/png" },
  } as unknown as SessionArtifact;
  assert.equal(visualDiffAspect(withoutDims), null);
});

test("computePixelDiff marks changed pixels above the JND threshold", () => {
  const before = pixelBuffer([
    [10, 20, 30, 255],
    [10, 20, 30, 255],
    [10, 20, 30, 255],
    [10, 20, 30, 255],
  ]);
  const after = pixelBuffer([
    [10, 20, 30, 255],   // identical
    [12, 22, 32, 255],   // sub-JND
    [200, 30, 30, 255],  // clearly different
    [10, 20, 30, 0],     // alpha drop to zero
  ]);
  const result = computePixelDiff(before, after, 2, 2);
  assert.equal(result.width, 2);
  assert.equal(result.height, 2);
  assert.equal(result.totalPixels, 4);
  assert.equal(result.changedPixels, 2);
  // Overlay index 0 (identical) — untouched
  assert.equal(result.overlay[3], 0);
  // Overlay index 2 (clearly different) — highlighted alpha
  assert.equal(result.overlay[8 + 3], 210);
});

test("computePixelDiff detects large single-channel deltas in every channel", () => {
  // Regression for the review-1 finding: weights were applied before squaring,
  // so a heavy blue delta (0.0722^2 * 166^2 = 143.7) sat just under the
  // threshold and black -> RGB(0,0,166) reported no change.
  const black = pixelBuffer([[0, 0, 0, 255]]);
  const cases: Array<{ name: string; after: [number, number, number, number] }> = [
    { name: "red channel jump", after: [166, 0, 0, 255] },
    { name: "green channel jump", after: [0, 166, 0, 255] },
    { name: "blue channel jump", after: [0, 0, 166, 255] },
  ];
  for (const { name, after } of cases) {
    const result = computePixelDiff(black, pixelBuffer([after]), 1, 1);
    assert.equal(result.changedPixels, 1, `${name}: expected change to be detected`);
    assert.equal(result.overlay[3], 210, `${name}: expected highlight alpha`);
  }
});

test("computePixelDiff detects alpha regressions including low-alpha shifts", () => {
  const opaque = pixelBuffer([[100, 100, 100, 255]]);
  const halfAlpha = pixelBuffer([[100, 100, 100, 128]]);
  const nearlyOpaque = pixelBuffer([[100, 100, 100, 245]]);
  const bothTransparent = pixelBuffer([[100, 100, 100, 0]]);
  const oneTransparent = pixelBuffer([[100, 100, 100, 0]]);

  const alphaDrop = computePixelDiff(opaque, halfAlpha, 1, 1);
  assert.equal(alphaDrop.changedPixels, 1, "half-alpha drop should register");

  const lowAlphaShift = computePixelDiff(opaque, nearlyOpaque, 1, 1);
  assert.equal(lowAlphaShift.changedPixels, 1, "10-unit alpha shift should register");

  const bothZero = computePixelDiff(bothTransparent, oneTransparent, 1, 1);
  assert.equal(bothZero.changedPixels, 0, "fully transparent pixels stay quiet");
});

test("boundedDiffDimensions returns floored dimensions when under the cap", () => {
  const bounds = boundedDiffDimensions(1024, 768);
  assert.equal(bounds.width, 1024);
  assert.equal(bounds.height, 768);
  assert.equal(bounds.scaled, false);
});

test("boundedDiffDimensions scales down oversized pairs preserving aspect", () => {
  // 8000 * 6000 = 48 MP, well past MAX_DIFF_PIXELS (~2 MP).
  const bounds = boundedDiffDimensions(8000, 6000);
  assert.equal(bounds.scaled, true);
  assert.ok(bounds.width * bounds.height <= MAX_DIFF_PIXELS);
  // Aspect within one pixel of 4:3.
  const ratio = bounds.width / bounds.height;
  assert.ok(Math.abs(ratio - 8000 / 6000) < 0.01);
  // Nothing collapses to zero.
  assert.ok(bounds.width > 0);
  assert.ok(bounds.height > 0);
});

test("boundedDiffDimensions guards non-positive inputs", () => {
  assert.deepEqual(boundedDiffDimensions(0, 100), { width: 0, height: 0, scaled: false });
  assert.deepEqual(boundedDiffDimensions(100, -1), { width: 0, height: 0, scaled: false });
  assert.deepEqual(boundedDiffDimensions(Number.NaN, 100), { width: 0, height: 0, scaled: false });
});

test("boundedDiffDimensions cap keeps 40MP inputs under a few million pixels", () => {
  // Backend allows 40 MP; ensure the cap catches it without allocating.
  const bounds = boundedDiffDimensions(8192, 4880);
  assert.ok(bounds.width * bounds.height <= MAX_DIFF_PIXELS);
  assert.equal(bounds.scaled, true);
});

test("computePixelDiff rejects mismatched buffer sizes", () => {
  const good = new Uint8ClampedArray(4);
  const bad = new Uint8ClampedArray(8);
  assert.throws(() => computePixelDiff(good, bad, 1, 1), /pixel buffers must be/);
});

test("formatDiffPercent formats human-friendly percentages", () => {
  assert.equal(formatDiffPercent(0, 0), "0%");
  assert.equal(formatDiffPercent(0, 100), "0%");
  assert.equal(formatDiffPercent(1, 100000), "<0.1%");
  assert.equal(formatDiffPercent(5, 100), "5.0%");
  assert.equal(formatDiffPercent(50, 100), "50%");
});
