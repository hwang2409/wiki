import assert from "node:assert/strict";
import test from "node:test";
import { formatAbsolute, formatRelative } from "../src/timestamp-format.ts";

const NOW = Date.parse("2026-07-10T12:00:00Z");

function ago(ms: number): string {
  return new Date(NOW - ms).toISOString();
}

function ahead(ms: number): string {
  return new Date(NOW + ms).toISOString();
}

test("formatRelative: sub-45s reads as 'just now'", () => {
  assert.equal(formatRelative(ago(0), NOW), "just now");
  assert.equal(formatRelative(ago(30_000), NOW), "just now");
  assert.equal(formatRelative(ago(44_999), NOW), "just now");
});

test("formatRelative: minutes bucket", () => {
  assert.equal(formatRelative(ago(45_000), NOW), "1m ago");
  assert.equal(formatRelative(ago(2 * 60_000), NOW), "2m ago");
  assert.equal(formatRelative(ago(59 * 60_000), NOW), "59m ago");
});

test("formatRelative: hours bucket", () => {
  assert.equal(formatRelative(ago(60 * 60_000), NOW), "1h ago");
  assert.equal(formatRelative(ago(3 * 3600_000), NOW), "3h ago");
  assert.equal(formatRelative(ago(23 * 3600_000), NOW), "23h ago");
});

test("formatRelative: yesterday and days", () => {
  assert.equal(formatRelative(ago(28 * 3600_000), NOW), "yesterday");
  assert.equal(formatRelative(ago(3 * 24 * 3600_000), NOW), "3d ago");
});

test("formatRelative: weeks / months / years", () => {
  assert.equal(formatRelative(ago(14 * 24 * 3600_000), NOW), "2 weeks ago");
  assert.equal(formatRelative(ago(60 * 24 * 3600_000), NOW), "2mo ago");
  assert.equal(formatRelative(ago(2 * 365 * 24 * 3600_000), NOW), "2y ago");
});

test("formatRelative: future", () => {
  assert.equal(formatRelative(ahead(0), NOW), "just now");
  assert.equal(formatRelative(ahead(5 * 60_000), NOW), "in 5m");
  assert.equal(formatRelative(ahead(3 * 3600_000), NOW), "in 3h");
  assert.equal(formatRelative(ahead(28 * 3600_000), NOW), "tomorrow");
});

test("formatRelative: empty on unparseable input", () => {
  assert.equal(formatRelative("", NOW), "");
  assert.equal(formatRelative("not-a-date", NOW), "");
});

test("formatAbsolute: contains ISO UTC and a local rendering", () => {
  const out = formatAbsolute("2026-07-10T12:34:56Z");
  assert.ok(out.includes("2026-07-10T12:34:56.000Z"), out);
  assert.ok(out.split("\n").length === 2, out);
});

test("formatAbsolute: passes through unparseable input", () => {
  assert.equal(formatAbsolute("garbage"), "garbage");
});
