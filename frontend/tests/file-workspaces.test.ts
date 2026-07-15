import test from "node:test";
import assert from "node:assert/strict";
import {
  filePanePath,
  fileResourceKey,
  isActiveFilePath,
  normalizeFilePanePath,
  parseFilePanePath,
  parseStoredRecentResources,
  workspaceFileSearchPath,
} from "../src/file-workspaces.ts";
import { fileContentRequestPath } from "../src/api.ts";

test("file pane paths round-trip workspace and relative path", () => {
  const first = filePanePath("wiki", "src/shared.ts");
  const second = filePanePath("phoebe", "src/shared.ts");

  assert.deepEqual(parseFilePanePath(first), { workspace: "wiki", path: "src/shared.ts" });
  assert.deepEqual(parseFilePanePath(second), { workspace: "phoebe", path: "src/shared.ts" });
  assert.notEqual(first, second);
  assert.notEqual(fileResourceKey("wiki", "src/shared.ts"), fileResourceKey("phoebe", "src/shared.ts"));
  assert.equal(workspaceFileSearchPath("phoebe", "src/shared.ts"), "phoebe/src/shared.ts");
  assert.equal(isActiveFilePath(first, "wiki", "src/shared.ts"), true);
  assert.equal(isActiveFilePath(first, "phoebe", "src/shared.ts"), false);
});

test("legacy persisted file paths normalize into the wiki workspace", () => {
  assert.equal(normalizeFilePanePath("src/shared.ts"), "file://wiki/src/shared.ts");
  assert.equal(normalizeFilePanePath("file://phoebe/src/shared.ts"), "file://phoebe/src/shared.ts");
});

test("recent resources migrate v1 entries and reject corrupt or unknown schemas", () => {
  assert.deepEqual(
    parseStoredRecentResources(
      JSON.stringify({ v: 1, entries: [{ kind: "file", path: "src/shared.ts" }, { kind: "note", path: "daily.md" }] })
    ),
    [
      { kind: "file", workspace: "wiki", path: "src/shared.ts" },
      { kind: "note", workspace: "wiki", path: "daily.md" },
    ]
  );
  assert.deepEqual(
    parseStoredRecentResources(JSON.stringify({ v: 2, entries: [{ kind: "file", workspace: "phoebe", path: "src/shared.ts" }] })),
    [{ kind: "file", workspace: "phoebe", path: "src/shared.ts" }]
  );
  assert.deepEqual(parseStoredRecentResources("not-json"), []);
  assert.deepEqual(parseStoredRecentResources(JSON.stringify({ v: 3, entries: [] })), []);
  assert.deepEqual(
    parseStoredRecentResources(JSON.stringify({ v: 2, entries: [{ kind: "file", workspace: "bad/workspace", path: "x" }] })),
    []
  );
});

test("code pane API requests preserve workspace and relative path separately", () => {
  assert.equal(
    fileContentRequestPath("phoebe", "src/shared.ts"),
    "/api/files/content?workspace=phoebe&path=src%2Fshared.ts"
  );
});
