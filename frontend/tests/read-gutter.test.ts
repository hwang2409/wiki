import assert from "node:assert/strict";
import test from "node:test";

import {
  numberedGutterWidth,
  parseNumberedPayload,
} from "../src/read-gutter.ts";

test("parses cat -n tab format", () => {
  const payload = parseNumberedPayload("1\tfoo\n2\tbar");
  assert.ok(payload);
  assert.deepEqual(payload!.lines, [
    { num: 1, text: "foo" },
    { num: 2, text: "bar" },
  ]);
  assert.equal(payload!.code, "foo\nbar");
});

test("parses arrow separator (Claude Read)", () => {
  const payload = parseNumberedPayload("740→export {};\n741→const x = 1;");
  assert.ok(payload);
  assert.deepEqual(payload!.lines, [
    { num: 740, text: "export {};" },
    { num: 741, text: "const x = 1;" },
  ]);
});

test("parses pipe separator", () => {
  const payload = parseNumberedPayload("1|first\n2|second");
  assert.ok(payload);
  assert.deepEqual(payload!.lines[0], { num: 1, text: "first" });
  assert.deepEqual(payload!.lines[1], { num: 2, text: "second" });
});

test("tolerates padded right-aligned numbers", () => {
  const payload = parseNumberedPayload("   1\tfoo\n  10\tbar\n 100\tbaz");
  assert.ok(payload);
  assert.deepEqual(payload!.lines.map((line) => line.num), [1, 10, 100]);
});

test("preserves blank lines as blank code without a fake line number", () => {
  const payload = parseNumberedPayload("1\tfoo\n\n3\tbaz");
  assert.ok(payload);
  assert.deepEqual(payload!.lines, [
    { num: 1, text: "foo" },
    { num: 0, text: "" },
    { num: 3, text: "baz" },
  ]);
  assert.equal(payload!.code, "foo\n\nbaz");
});

test("returns null when any non-blank line lacks the gutter", () => {
  // A leading harness message before the numbered block is a mixed payload;
  // fall through to plain rendering rather than misalign the gutter column.
  assert.equal(parseNumberedPayload("[note] head\n1\tfoo\n2\tbar"), null);
  assert.equal(parseNumberedPayload("1\tfoo\ngarbled tail\n3\tbaz"), null);
});

test("returns null for pure plain text (no numbered lines at all)", () => {
  assert.equal(parseNumberedPayload("plain\nwords"), null);
});

test("returns null for empty input", () => {
  assert.equal(parseNumberedPayload(""), null);
});

test("ignores a single trailing newline artifact", () => {
  const payload = parseNumberedPayload("1\tfoo\n2\tbar\n");
  assert.ok(payload);
  assert.equal(payload!.lines.length, 2);
});

test("drops the harness truncation marker on the final line", () => {
  // Backend truncates long file reads and appends `… [N chars truncated]`
  // on its own line. Without this drop the whole payload fails the parse
  // and every long read loses highlighting.
  const payload = parseNumberedPayload(
    "1\tfoo\n2\tbar\n\n… [1727 chars truncated]",
  );
  assert.ok(payload);
  assert.deepEqual(payload!.lines, [
    { num: 1, text: "foo" },
    { num: 2, text: "bar" },
  ]);
});

test("drops an ASCII-dot truncation marker", () => {
  const payload = parseNumberedPayload("1\tfoo\n... [42 chars truncated]");
  assert.ok(payload);
  assert.equal(payload!.lines.length, 1);
});

test("preserves code that contains the separator characters", () => {
  // Code may legitimately hold `|`, `\t`, or `→`; the separator match is on
  // the FIRST occurrence, so code retains everything after it.
  const payload = parseNumberedPayload("1\tif (a || b) return;");
  assert.ok(payload);
  assert.deepEqual(payload!.lines[0], { num: 1, text: "if (a || b) return;" });
});

test("numberedGutterWidth returns the widest line-number width", () => {
  const payload = parseNumberedPayload("1\ta\n999\tb\n10\tc");
  assert.ok(payload);
  assert.equal(numberedGutterWidth(payload!), 3);
});

test("numberedGutterWidth floors at 1 for single-digit payloads", () => {
  const payload = parseNumberedPayload("1\ta");
  assert.ok(payload);
  assert.equal(numberedGutterWidth(payload!), 1);
});
