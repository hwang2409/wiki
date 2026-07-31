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

// Perceived-luminance-weighted RGB delta. Alpha is respected so fully
// transparent pixels never register as "changed". Squared distance keeps the
// math cheap and avoids sqrt in the hot loop.
const R_WEIGHT = 0.2126;
const G_WEIGHT = 0.7152;
const B_WEIGHT = 0.0722;

function weightedDelta(
  rA: number, gA: number, bA: number, aA: number,
  rB: number, gB: number, bB: number, aB: number,
): number {
  if (aA === 0 && aB === 0) return 0;
  const dr = (rA - rB) * R_WEIGHT;
  const dg = (gA - gB) * G_WEIGHT;
  const db = (bA - bB) * B_WEIGHT;
  const da = (aA - aB) / 255;
  return dr * dr + dg * dg + db * db + da * da * 255 * 255;
}

// Threshold roughly maps to the JND (just-noticeable difference) on typical
// UI screenshots. Bumped high enough that JPEG artifacts don't paint the
// canvas red, low enough that a one-pixel color change registers.
const DIFF_THRESHOLD_SQUARED = 12 * 12;

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

export function formatDiffPercent(changed: number, total: number): string {
  if (total === 0) return "0%";
  const percent = (changed / total) * 100;
  if (percent === 0) return "0%";
  if (percent < 0.1) return "<0.1%";
  return `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
}
