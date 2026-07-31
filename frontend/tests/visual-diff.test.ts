import assert from "node:assert/strict";
import test from "node:test";
import {
  blendOpacity,
  computePixelDiff,
  formatDiffPercent,
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
