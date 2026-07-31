// Shared helpers for the visual-diff artifact kind (WIKI-193).
// The pure pieces live here so vitest can cover the pixel-diff and blend math
// without spinning up a real canvas or browser.

import type { SessionArtifact, SessionEvent } from "./api";

export type VisualDiffVariant = "before" | "after";

export type VisualDiffSources = {
  before: string;
  after: string;
};

export function visualDiffSources(
  ticket: string,
  event: SessionEvent,
): VisualDiffSources {
  const base = `/api/agents/${encodeURIComponent(ticket)}/artifact/${encodeURIComponent(event.artifact_id ?? "")}`;
  return {
    before: `${base}?variant=before`,
    after: `${base}?variant=after`,
  };
}

export function visualDiffAspect(artifact: SessionArtifact): number | null {
  const width = artifact.before?.width ?? artifact.after?.width;
  const height = artifact.before?.height ?? artifact.after?.height;
  if (!width || !height) return null;
  return width / height;
}

// Slider position is a 0..1 value where 0 = 100% before, 1 = 100% after.
export function blendOpacity(sliderPosition: number): number {
  if (!Number.isFinite(sliderPosition)) return 0;
  return Math.min(1, Math.max(0, sliderPosition));
}

// Perceptual color distance. Weights are applied AFTER squaring — otherwise
// a heavy blue-channel change (small weight) squares its own weight and
// disappears (see WIKI-193 review: black -> rgb(0,0,166) reported no change).
// Alpha delta is compared on a 0..255 scale so a full opacity flip always
// registers, and fully-transparent pixels never register as "changed" since
// their color values are not visible.
const R_WEIGHT = 0.30;
const G_WEIGHT = 0.59;
const B_WEIGHT = 0.11;
// Alpha is disproportionately visible on transparent overlays (a 4% opacity
// shift is obvious against a checkerboard), so it gets extra weight.
const ALPHA_WEIGHT = 2.0;

function weightedDelta(
  rA: number, gA: number, bA: number, aA: number,
  rB: number, gB: number, bB: number, aB: number,
): number {
  if (aA === 0 && aB === 0) return 0;
  // Premultiply RGB by alpha — unpremultiplied color channels under low or
  // zero alpha do not paint pixels on screen. Comparing raw RGB there
  // reports "hidden" differences as visible regressions and paints false
  // highlights across transparent screenshot regions. Alpha keeps its own
  // delta term so opacity flips still register even when RGB is unchanged.
  const factorA = aA / 255;
  const factorB = aB / 255;
  const dr = rA * factorA - rB * factorB;
  const dg = gA * factorA - gB * factorB;
  const db = bA * factorA - bB * factorB;
  const da = aA - aB;
  return (
    R_WEIGHT * dr * dr
    + G_WEIGHT * dg * dg
    + B_WEIGHT * db * db
    + ALPHA_WEIGHT * da * da
  );
}

// Threshold is calibrated against the weighted squared-channel metric above.
// A single channel changing by ~10 units in luminance-dominant green
// (0.59 * 100 = 59) sits below the threshold; anything larger paints. This
// keeps JPEG blocking artifacts quiet while catching every visible UI change.
const DIFF_THRESHOLD_SQUARED = 100;

export type PixelDiffResult = {
  width: number;
  height: number;
  changedPixels: number;
  totalPixels: number;
  overlay: Uint8ClampedArray;
};

const HIGHLIGHT_R = 255;
const HIGHLIGHT_G = 60;
const HIGHLIGHT_B = 120;
const HIGHLIGHT_A = 210;

export function computePixelDiff(
  before: Uint8ClampedArray,
  after: Uint8ClampedArray,
  width: number,
  height: number,
): PixelDiffResult {
  const totalPixels = width * height;
  const expected = totalPixels * 4;
  if (before.length !== expected || after.length !== expected) {
    throw new Error(
      `pixel buffers must be ${expected} bytes (got before=${before.length}, after=${after.length})`,
    );
  }
  const overlay = new Uint8ClampedArray(expected);
  let changedPixels = 0;
  for (let i = 0; i < expected; i += 4) {
    const delta = weightedDelta(
      before[i], before[i + 1], before[i + 2], before[i + 3],
      after[i], after[i + 1], after[i + 2], after[i + 3],
    );
    if (delta > DIFF_THRESHOLD_SQUARED) {
      overlay[i] = HIGHLIGHT_R;
      overlay[i + 1] = HIGHLIGHT_G;
      overlay[i + 2] = HIGHLIGHT_B;
      overlay[i + 3] = HIGHLIGHT_A;
      changedPixels += 1;
    }
  }
  return { width, height, changedPixels, totalPixels, overlay };
}

// Backend allows up to 40 MP; a single naive diff at that resolution
// allocates ~500 MB across scratch canvases and RGBA buffers and blocks
// the main thread. The renderer diffs oversized pairs in native-resolution
// tiles of at most NATIVE_DIFF_TILE per side and composites changed
// pixels onto a bounded overlay canvas so the visible layer stays cheap
// while the stats stay accurate. MAX_DIFF_PIXELS remains as the overlay
// cap only — never the comparison cap.
export const NATIVE_DIFF_TILE = 1024;
export const MAX_DIFF_PIXELS = 2_000_000;

export function boundedDiffDimensions(
  width: number,
  height: number,
  maxPixels: number = MAX_DIFF_PIXELS,
): { width: number; height: number; scaled: boolean } {
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return { width: 0, height: 0, scaled: false };
  }
  const pixels = width * height;
  if (pixels <= maxPixels) {
    return { width: Math.floor(width), height: Math.floor(height), scaled: false };
  }
  const scale = Math.sqrt(maxPixels / pixels);
  const w = Math.max(1, Math.floor(width * scale));
  const h = Math.max(1, Math.floor(height * scale));
  return { width: w, height: h, scaled: true };
}

export function formatDiffPercent(changed: number, total: number): string {
  if (total === 0) return "0%";
  const percent = (changed / total) * 100;
  if (percent === 0) return "0%";
  if (percent < 0.1) return "<0.1%";
  return `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
}
