// @vitest-environment jsdom
// WIKI-200 — the session scope LRU must stay bounded.

import { afterEach, describe, expect, test } from "vitest";

import {
  __markSeenArtifactForTests,
  __resetSeenArtifactsForTests,
  __seenArtifactScopeCountForTests,
} from "../src/artifact-block";

afterEach(() => {
  __resetSeenArtifactsForTests();
});

describe("artifact animation scope cache", () => {
  test("U7: evicts cold session scopes instead of growing forever", () => {
    for (let index = 0; index < 100; index += 1) {
      expect(__markSeenArtifactForTests(`session-${index}`, "artifact")).toBe(true);
    }

    expect(__seenArtifactScopeCountForTests()).toBeLessThanOrEqual(64);
    expect(__markSeenArtifactForTests("session-0", "artifact")).toBe(true);
  });
});
