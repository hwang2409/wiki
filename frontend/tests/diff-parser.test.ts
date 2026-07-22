import assert from "node:assert/strict";
import test from "node:test";
import {
  fileKind,
  fileTitle,
  parseUnifiedDiff,
  type DiffFilePatch,
} from "../src/diff-parser.ts";

function fileByIndex(files: DiffFilePatch[], index: number): DiffFilePatch {
  const file = files[index];
  assert.ok(file, `expected file at index ${index}`);
  return file;
}

test("empty source returns no files", () => {
  assert.deepEqual(parseUnifiedDiff(""), []);
});

test("diff --git patch with single hunk parses paths and lines", () => {
  const source = [
    "diff --git a/frontend/src/example.tsx b/frontend/src/example.tsx",
    "index 111111..222222 100644",
    "--- a/frontend/src/example.tsx",
    "+++ b/frontend/src/example.tsx",
    "@@ -1,4 +1,5 @@",
    " import { StatusBadge } from './status-badge';",
    "-export const width = 720;",
    "+export const width = 760;",
    "+export const readable = true;",
    " export const enabled = true;",
    "",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const file = fileByIndex(files, 0);
  assert.equal(file.oldPath, "frontend/src/example.tsx");
  assert.equal(file.newPath, "frontend/src/example.tsx");
  assert.equal(file.hunks.length, 1);
  const kinds = file.hunks[0].lines.map((line) => line.kind);
  assert.deepEqual(kinds, ["context", "remove", "add", "add", "context"]);
});

test("plain diff -u multi-file (no diff --git) splits into separate files", () => {
  const source = [
    "--- a/first.txt",
    "+++ b/first.txt",
    "@@ -1,2 +1,2 @@",
    " keep",
    "-old",
    "+new",
    "--- a/second.txt",
    "+++ b/second.txt",
    "@@ -1,1 +1,2 @@",
    " keep",
    "+extra",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 2, `expected 2 files, got ${files.length}`);
  assert.equal(files[0].newPath, "first.txt");
  assert.equal(files[1].newPath, "second.txt");
  assert.equal(files[0].hunks.length, 1);
  assert.equal(files[1].hunks.length, 1);
});

test("hunk content lines beginning with -- and ++ are not misclassified as file headers", () => {
  const source = [
    "diff --git a/notes.txt b/notes.txt",
    "--- a/notes.txt",
    "+++ b/notes.txt",
    "@@ -1,3 +1,3 @@",
    "-- keep-two-dashes",
    "++ add-two-pluses",
    " tail",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const file = fileByIndex(files, 0);
  assert.equal(file.hunks.length, 1);
  const lines = file.hunks[0].lines;
  assert.equal(lines.length, 3);
  assert.equal(lines[0].kind, "remove");
  assert.equal(lines[0].text, "- keep-two-dashes");
  assert.equal(lines[1].kind, "add");
  assert.equal(lines[1].text, "+ add-two-pluses");
  assert.equal(lines[2].kind, "context");
});

test("subsequent file headers after a hunk end the hunk cleanly", () => {
  const source = [
    "diff --git a/a.txt b/a.txt",
    "--- a/a.txt",
    "+++ b/a.txt",
    "@@ -1,1 +1,1 @@",
    "-old",
    "+new",
    "diff --git a/b.txt b/b.txt",
    "--- a/b.txt",
    "+++ b/b.txt",
    "@@ -1,1 +1,1 @@",
    "-x",
    "+y",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 2);
  assert.equal(files[0].newPath, "a.txt");
  assert.equal(files[1].newPath, "b.txt");
  assert.equal(files[0].hunks[0].lines.length, 2);
  assert.equal(files[1].hunks[0].lines.length, 2);
});

test("binary-file patch is preserved with extended header and no hunks", () => {
  const source = [
    "diff --git a/logo.png b/logo.png",
    "index 1111111..2222222 100644",
    "Binary files a/logo.png and b/logo.png differ",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const file = fileByIndex(files, 0);
  assert.equal(file.hunks.length, 0);
  assert.equal(fileKind(file), "binary");
  assert.equal(fileTitle(file), "logo.png");
  assert.ok(
    file.extendedHeaders.some((line) => line.startsWith("Binary files ")),
    "binary header should be captured",
  );
});

test("rename-only patch surfaces both paths and renamed kind", () => {
  const source = [
    "diff --git a/old-name.txt b/new-name.txt",
    "similarity index 100%",
    "rename from old-name.txt",
    "rename to new-name.txt",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const file = fileByIndex(files, 0);
  assert.equal(file.hunks.length, 0);
  assert.equal(fileKind(file), "renamed");
  assert.equal(fileTitle(file), "old-name.txt → new-name.txt");
});

test("new file patch reports new kind", () => {
  const source = [
    "diff --git a/added.txt b/added.txt",
    "new file mode 100644",
    "index 0000000..1111111",
    "--- /dev/null",
    "+++ b/added.txt",
    "@@ -0,0 +1,1 @@",
    "+hello",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const file = fileByIndex(files, 0);
  assert.equal(fileKind(file), "new");
  assert.equal(fileTitle(file), "added.txt");
  assert.equal(file.oldPath, null);
});

test("terminal newline in source does not produce a phantom trailing line", () => {
  const withNewline = [
    "diff --git a/foo.txt b/foo.txt",
    "--- a/foo.txt",
    "+++ b/foo.txt",
    "@@ -1,2 +1,2 @@",
    " keep",
    "-old",
    "+new",
    "",
  ].join("\n");
  const files = parseUnifiedDiff(withNewline);
  assert.equal(files.length, 1);
  const lines = files[0].hunks[0].lines;
  assert.equal(lines.length, 3, `expected 3 hunk lines, got ${lines.length}`);
  assert.deepEqual(lines.map((line) => line.kind), ["context", "remove", "add"]);
});

test("missing terminal newline retains the last line", () => {
  const withoutNewline = [
    "diff --git a/foo.txt b/foo.txt",
    "--- a/foo.txt",
    "+++ b/foo.txt",
    "@@ -1,2 +1,2 @@",
    " keep",
    "-old",
    "+new",
  ].join("\n");
  const files = parseUnifiedDiff(withoutNewline);
  assert.equal(files.length, 1);
  const lines = files[0].hunks[0].lines;
  assert.equal(lines.length, 3);
  assert.equal(lines[2].kind, "add");
  assert.equal(lines[2].text, "new");
});

test("blank context line inside a hunk is preserved (not filtered)", () => {
  const source = [
    "diff --git a/foo.txt b/foo.txt",
    "--- a/foo.txt",
    "+++ b/foo.txt",
    "@@ -1,4 +1,4 @@",
    " head",
    " ",
    " tail",
    "-old",
    "+new",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const lines = files[0].hunks[0].lines;
  assert.equal(lines.length, 5);
  assert.equal(lines[1].kind, "context");
  assert.equal(lines[1].text, "");
});

test("\\ No newline at end of file marker in a hunk does not consume quota", () => {
  const source = [
    "diff --git a/foo.txt b/foo.txt",
    "--- a/foo.txt",
    "+++ b/foo.txt",
    "@@ -1,2 +1,2 @@",
    "-old",
    "\\ No newline at end of file",
    " tail",
    "+new",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const kinds = files[0].hunks[0].lines.map((line) => line.kind);
  assert.deepEqual(kinds, ["remove", "meta", "context", "add"]);
});

test("trailing \\ No newline marker after the final counted line is preserved", () => {
  const source = [
    "diff --git a/foo.txt b/foo.txt",
    "--- a/foo.txt",
    "+++ b/foo.txt",
    "@@ -1,2 +1,2 @@",
    " keep",
    "-old",
    "+new",
    "\\ No newline at end of file",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 1);
  const lines = files[0].hunks[0].lines;
  assert.equal(lines.length, 4, `expected trailing meta line preserved, got ${lines.length}`);
  const last = lines[lines.length - 1];
  assert.equal(last.kind, "meta");
  assert.equal(last.text, "\\ No newline at end of file");
});

test("trailing \\ No newline marker is attributed to preceding file, not the next", () => {
  const source = [
    "diff --git a/a.txt b/a.txt",
    "--- a/a.txt",
    "+++ b/a.txt",
    "@@ -1,1 +1,1 @@",
    "-old",
    "+new",
    "\\ No newline at end of file",
    "diff --git a/b.txt b/b.txt",
    "--- a/b.txt",
    "+++ b/b.txt",
    "@@ -1,1 +1,1 @@",
    "-x",
    "+y",
  ].join("\n");
  const files = parseUnifiedDiff(source);
  assert.equal(files.length, 2);
  const firstLines = files[0].hunks[0].lines;
  assert.equal(firstLines[firstLines.length - 1].kind, "meta");
  assert.equal(files[1].hunks[0].lines.length, 2);
});
