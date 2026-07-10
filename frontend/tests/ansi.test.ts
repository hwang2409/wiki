import assert from "node:assert/strict";
import test from "node:test";
import { hasAnsi, parseAnsi } from "../src/ansi";

test("plain text is a single unstyled segment", () => {
  const segs = parseAnsi("hello world");
  assert.deepEqual(segs, [{ text: "hello world", style: {} }]);
  assert.equal(hasAnsi("hello world"), false);
});

test("basic fg color + reset", () => {
  const segs = parseAnsi("\x1b[32m✓\x1b[0m PASSED");
  assert.deepEqual(segs, [
    { text: "✓", style: { fg: 2 } },
    { text: " PASSED", style: {} },
  ]);
});

test("bold + underline + bright fg + reset", () => {
  const segs = parseAnsi("\x1b[1;4;91mFAIL\x1b[0mok");
  assert.deepEqual(segs, [
    { text: "FAIL", style: { bold: true, underline: true, fg: 9 } },
    { text: "ok", style: {} },
  ]);
});

test("bg color 40-47", () => {
  const segs = parseAnsi("\x1b[41mred-bg\x1b[49mnormal");
  assert.deepEqual(segs, [
    { text: "red-bg", style: { bg: 1 } },
    { text: "normal", style: {} },
  ]);
});

test("empty CSI ([m) resets", () => {
  const segs = parseAnsi("\x1b[32mgreen\x1b[mplain");
  assert.deepEqual(segs, [
    { text: "green", style: { fg: 2 } },
    { text: "plain", style: {} },
  ]);
});

test("adjacent same-style segments coalesce", () => {
  const segs = parseAnsi("\x1b[31mred\x1b[31m-more\x1b[0m");
  assert.deepEqual(segs, [{ text: "red-more", style: { fg: 1 } }]);
});

test("256-color escapes are stripped, subsequent styling still applies", () => {
  const segs = parseAnsi("\x1b[38;5;196mstill-red\x1b[1mbold\x1b[0m");
  assert.deepEqual(segs, [
    { text: "still-red", style: {} },
    { text: "bold", style: { bold: true } },
  ]);
});

test("truecolor escapes are stripped", () => {
  const segs = parseAnsi("\x1b[38;2;255;0;0mrgb\x1b[0m");
  assert.deepEqual(segs, [{ text: "rgb", style: {} }]);
});

test("cursor-move CSI is swallowed without emitting text", () => {
  const segs = parseAnsi("A\x1b[2Jclear\x1b[Hhome");
  assert.deepEqual(segs, [{ text: "Aclearhome", style: {} }]);
});

test("OSC sequences (terminated by BEL) are stripped", () => {
  const segs = parseAnsi("before\x1b]0;title\x07after");
  assert.deepEqual(segs, [{ text: "beforeafter", style: {} }]);
});

test("OSC terminated by ESC \\ is stripped", () => {
  const segs = parseAnsi("a\x1b]0;t\x1b\\b");
  assert.deepEqual(segs, [{ text: "ab", style: {} }]);
});

test("preserves newlines", () => {
  const segs = parseAnsi("\x1b[32mline1\nline2\x1b[0m\ntail");
  assert.deepEqual(segs, [
    { text: "line1\nline2", style: { fg: 2 } },
    { text: "\ntail", style: {} },
  ]);
});

test("hasAnsi detects escape presence", () => {
  assert.equal(hasAnsi("\x1b[31mx"), true);
  assert.equal(hasAnsi("plain"), false);
});

test("22/23/24 reverts only the targeted attribute", () => {
  const segs = parseAnsi("\x1b[1;3;4mABC\x1b[22mDE\x1b[0m");
  assert.deepEqual(segs, [
    { text: "ABC", style: { bold: true, italic: true, underline: true } },
    { text: "DE", style: { italic: true, underline: true } },
  ]);
});
