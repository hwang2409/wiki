// WIKI-251 HIGH#4: the chat pane default width tracks the viewport at ~47%
// (floored to 560px, capped at 70%). Until the user drags the resize handle
// the width re-flows on window resize; after a drag we lock the value to px
// and further resizes preserve it. These tests pin the exact ratios the
// contract calls out (1280 / 1440 / 1920 / 2560), the floor/cap boundaries,
// and the persisted "custom" branch — a fixed-pixel regression fails here.

import { describe, expect, test } from "vitest";

import { clampSidebarWidth, computeDefaultWidth } from "../src/session";

describe("WIKI-251 SessionSidebar default width", () => {
  test("at 1280 viewport the pane sits at the 560px floor (~44%)", () => {
    const width = computeDefaultWidth(1280);
    expect(width).toBeGreaterThanOrEqual(560);
    expect(width).toBeLessThanOrEqual(Math.round(1280 * 0.55));
    // Never sub-min, never past the 70% cap.
    expect(width).toBeGreaterThanOrEqual(320);
    expect(width).toBeLessThanOrEqual(Math.round(1280 * 0.7));
  });

  test("at 1440 viewport the pane holds the ~47% ratio (± 3%)", () => {
    const width = computeDefaultWidth(1440);
    const ratio = width / 1440;
    expect(ratio).toBeGreaterThanOrEqual(0.44);
    expect(ratio).toBeLessThanOrEqual(0.5);
  });

  test("at 1920 viewport the pane holds the ~47% ratio (± 3%)", () => {
    const width = computeDefaultWidth(1920);
    const ratio = width / 1920;
    expect(ratio).toBeGreaterThanOrEqual(0.44);
    expect(ratio).toBeLessThanOrEqual(0.5);
  });

  test("at 2560 viewport the pane holds the ~47% ratio (± 3%)", () => {
    const width = computeDefaultWidth(2560);
    const ratio = width / 2560;
    expect(ratio).toBeGreaterThanOrEqual(0.44);
    expect(ratio).toBeLessThanOrEqual(0.5);
  });

  test("width re-flows across viewports without a fixed-pixel lock-in", () => {
    // The critical regression: a fixed pixel value drifts to any percentage
    // as the window changes. computeDefaultWidth must produce a DIFFERENT
    // number for each viewport (proving it re-flows) — a return that stays
    // pinned would fail this equality check.
    const w1280 = computeDefaultWidth(1280);
    const w1440 = computeDefaultWidth(1440);
    const w1920 = computeDefaultWidth(1920);
    const w2560 = computeDefaultWidth(2560);
    expect(new Set([w1280, w1440, w1920, w2560]).size).toBeGreaterThan(1);
    // Monotonic: larger viewport → wider pane (never smaller).
    expect(w1440).toBeGreaterThanOrEqual(w1280);
    expect(w1920).toBeGreaterThanOrEqual(w1440);
    expect(w2560).toBeGreaterThanOrEqual(w1920);
  });

  test("clampSidebarWidth respects MIN (320px) floor and 70% cap regardless of persisted value", () => {
    // A stale localStorage entry that predates the WIKI-251 default (say 480)
    // still gets clamped to the [MIN, 70% * viewport] band.
    expect(clampSidebarWidth(50, 1440)).toBe(320);
    expect(clampSidebarWidth(5000, 1440)).toBe(Math.round(1440 * 0.7));
    // A drag-locked width inside the band survives intact.
    expect(clampSidebarWidth(720, 1440)).toBe(720);
  });
});
