import test from "node:test";
import assert from "node:assert/strict";
import {
  buildTree,
  filePanePath,
  fileResourceKey,
  isActiveFilePath,
  normalizeFilePanePath,
  parseFilePanePath,
  parseStoredRecentResources,
  reconcileWorkspaceState,
  shouldAcceptWorkspaceResponse,
  shouldDiscoverWorkspaces,
  WorkspaceRequestTracker,
  workspaceFileSearchPath,
  workspaceCacheKey,
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

test("built trees preserve workspace identity for shared relative paths", () => {
  const files = [{ path: "src/shared.ts" }];
  const wikiTree = buildTree([], files, "wiki");
  const phoebeTree = buildTree([], files, "phoebe");
  const wikiFile = wikiTree.folders[0]?.files[0];
  const phoebeFile = phoebeTree.folders[0]?.files[0];

  assert.equal(wikiFile?.path, "src/shared.ts");
  assert.equal(wikiFile?.workspace, "wiki");
  assert.equal(phoebeFile?.path, "src/shared.ts");
  assert.equal(phoebeFile?.workspace, "phoebe");
  assert.equal(phoebeFile?.openPath, filePanePath("phoebe", "src/shared.ts"));
});

test("workspace refresh reconciles stopped and newly live workspaces", () => {
  const wiki = { id: "wiki", root: "/wiki", live: true };
  const phoebe = { id: "phoebe", root: "/phoebe", live: true };
  const cache = { [workspaceCacheKey(wiki)]: "wiki-files", [workspaceCacheKey(phoebe)]: "phoebe-files" };
  const stopped = reconcileWorkspaceState(
    [
      wiki,
      { ...phoebe, live: false },
    ],
    "phoebe",
    cache
  );
  assert.equal(stopped.activeWorkspace, "wiki");
  assert.deepEqual(stopped.cache, { [workspaceCacheKey(wiki)]: "wiki-files" });

  const newlyLive = reconcileWorkspaceState(
    [
      wiki,
      phoebe,
    ],
    "phoebe",
    stopped.cache
  );
  assert.equal(newlyLive.activeWorkspace, "phoebe");
  assert.deepEqual(newlyLive.cache, { [workspaceCacheKey(wiki)]: "wiki-files" });
});

test("workspace cache keys invalidate same-ID root changes", () => {
  const oldWorkspace = { id: "misc", root: "/old", live: true };
  const newWorkspace = { id: "misc", root: "/new", live: true };
  const oldKey = workspaceCacheKey(oldWorkspace);
  const next = reconcileWorkspaceState([newWorkspace], "misc", { [oldKey]: "stale" });

  assert.equal(next.activeWorkspace, "misc");
  assert.deepEqual(next.cache, {});
  assert.notEqual(oldKey, workspaceCacheKey(newWorkspace));
});

test("delayed file responses are ignored after a workspace lifecycle refresh", () => {
  const workspace = { id: "phoebe", root: "/phoebe", live: true };
  const key = workspaceCacheKey(workspace);

  assert.equal(shouldAcceptWorkspaceResponse(4, 4, workspace, key), true);
  assert.equal(shouldAcceptWorkspaceResponse(4, 5, { ...workspace, live: false }, key), false);
});

test("workspace reactivation replaces a stale in-flight request", () => {
  const tracker = new WorkspaceRequestTracker();
  const first = tracker.begin("phoebe\u0000/phoebe");
  assert.ok(first);

  tracker.invalidate();
  const replacement = tracker.begin("phoebe\u0000/phoebe");
  assert.ok(replacement);
  assert.notEqual(first.token, replacement.token);
  assert.equal(tracker.isCurrent("phoebe\u0000/phoebe", first), false);
  assert.equal(tracker.isCurrent("phoebe\u0000/phoebe", replacement), true);

  tracker.finish("phoebe\u0000/phoebe", first);
  assert.equal(tracker.isCurrent("phoebe\u0000/phoebe", replacement), true);
});

test("switcher opening bootstraps workspaces from persisted non-files tabs", () => {
  assert.equal(shouldDiscoverWorkspaces("search", false), false);
  assert.equal(shouldDiscoverWorkspaces("agents", false), false);
  assert.equal(shouldDiscoverWorkspaces("search", true), true);
  assert.equal(shouldDiscoverWorkspaces("agents", true), true);
  assert.equal(shouldDiscoverWorkspaces("files", false), true);
});
