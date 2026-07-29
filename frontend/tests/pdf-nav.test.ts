import assert from "node:assert/strict";
import test from "node:test";
import {
  ZOOM_STEPS,
  applyKeyNav,
  focalPreservedScroll,
  resolveKeyNav,
  resolveZoom,
  snapToZoomStep,
} from "../src/pdf-nav.ts";

test("canonical zoom step ladder includes 175%", () => {
  assert.deepEqual(
    [...ZOOM_STEPS],
    [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2],
  );
});

test("snapToZoomStep advances to next canonical step", () => {
  assert.equal(snapToZoomStep(1, 1), 1.25);
  assert.equal(snapToZoomStep(1.5, 1), 1.75);
  assert.equal(snapToZoomStep(1.75, 1), 2);
  assert.equal(snapToZoomStep(2, 1), 2);
});

test("snapToZoomStep steps down to previous canonical step", () => {
  assert.equal(snapToZoomStep(1.75, -1), 1.5);
  assert.equal(snapToZoomStep(1, -1), 0.75);
  assert.equal(snapToZoomStep(0.5, -1), 0.5);
});

test("resolveZoom fit-width divides viewport width minus padding by page width", () => {
  const zoom = resolveZoom("fit-width", { width: 800, height: 600 }, { width: 400, height: 300 });
  assert.equal(zoom, (800 - 48) / 400);
});

test("resolveZoom fit-page respects both dimensions", () => {
  const zoom = resolveZoom("fit-page", { width: 400, height: 300 }, { width: 400, height: 400 });
  assert.equal(zoom, (300 - 48) / 400);
});

test("resolveKeyNav ignores keyboard events when the target is editable", () => {
  assert.equal(
    resolveKeyNav({ key: "ArrowRight", targetIsEditable: true }),
    null,
  );
});

test("resolveKeyNav returns prev/next for bare arrows", () => {
  assert.deepEqual(resolveKeyNav({ key: "ArrowLeft" }), { kind: "prev" });
  assert.deepEqual(resolveKeyNav({ key: "ArrowRight" }), { kind: "next" });
});

test("resolveKeyNav returns first/last for cmd+arrow (mac) and ctrl+arrow (linux)", () => {
  assert.deepEqual(resolveKeyNav({ key: "ArrowLeft", metaKey: true }), { kind: "first" });
  assert.deepEqual(resolveKeyNav({ key: "ArrowRight", metaKey: true }), { kind: "last" });
  assert.deepEqual(resolveKeyNav({ key: "ArrowLeft", ctrlKey: true }), { kind: "first" });
  assert.deepEqual(resolveKeyNav({ key: "ArrowRight", ctrlKey: true }), { kind: "last" });
});

test("resolveKeyNav returns open-find for cmd+f", () => {
  assert.deepEqual(resolveKeyNav({ key: "f", metaKey: true }), { kind: "open-find" });
  assert.deepEqual(resolveKeyNav({ key: "F", ctrlKey: true }), { kind: "open-find" });
});

test("applyKeyNav clamps to first and last pages", () => {
  assert.equal(applyKeyNav({ kind: "prev" }, 1, 10), 1);
  assert.equal(applyKeyNav({ kind: "next" }, 10, 10), 10);
  assert.equal(applyKeyNav({ kind: "first" }, 5, 10), 1);
  assert.equal(applyKeyNav({ kind: "last" }, 3, 10), 10);
  assert.equal(applyKeyNav({ kind: "open-find" }, 4, 10), 4);
});

test("focalPreservedScroll keeps the viewport center anchored across zoom changes", () => {
  const input = {
    viewportWidth: 800,
    viewportHeight: 600,
    scrollLeft: 200,
    scrollTop: 100,
    currentZoom: 1,
    nextZoom: 2,
  };
  const result = focalPreservedScroll(input);
  // Center in page coordinates before zoom: ((200 + 400)/1, (100 + 300)/1) = (600, 400).
  // After zoom to 2x, that center at 2x lands at 1200,800; scroll needed to keep
  // the same center under the viewport midpoint is (1200 - 400, 800 - 300) = (800, 500).
  assert.equal(result.scrollLeft, 800);
  assert.equal(result.scrollTop, 500);
});

test("focalPreservedScroll clamps to zero when the anchor is above the viewport", () => {
  const result = focalPreservedScroll({
    viewportWidth: 800,
    viewportHeight: 600,
    scrollLeft: 0,
    scrollTop: 0,
    currentZoom: 2,
    nextZoom: 1,
  });
  assert.equal(result.scrollLeft, 0);
  assert.equal(result.scrollTop, 0);
});
