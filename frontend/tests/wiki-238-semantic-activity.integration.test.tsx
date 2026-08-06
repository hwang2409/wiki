// WIKI-238 run-state labels. The aggregated turn header (semantic summaries,
// counts, elapsed) was removed in WIKI-247 — only the live-state helpers that
// still drive CurrentTurnState remain under test.
import { describe, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import {
  activityRunStateFromProvider,
  activityStateLabel,
} from "../src/agent-events";

function toolEvent(
  archetype: string,
  summary: string,
  overrides: Partial<SessionTool> = {},
  ts = "2026-08-02T12:00:00.000Z",
): SessionEvent {
  return {
    id: 1,
    kind: "tool",
    ts,
    text: "",
    disposition: "rendered",
    tool: {
      name: "fixture_tool",
      input: "fixture input",
      output: "fixture output",
      ok: true,
      archetype,
      summary,
      ...overrides,
    },
  };
}

describe("model activity run states", () => {
  test("labels working, done, failed, and waiting-for-you states", () => {
    const completed = [toolEvent("read", "read api.ts")];
    const failed = [toolEvent("validate", "pytest", { ok: false })];
    expect(activityStateLabel(completed, "working")).toBe("working");
    expect(activityStateLabel(completed)).toBe("done");
    expect(activityStateLabel([toolEvent("read", "read api.ts", { output: null, ok: null })])).toBe("working");
    expect(activityStateLabel(failed)).toBe("failed");
    expect(activityStateLabel([
      toolEvent("validate", "pytest first attempt", { ok: false }),
      toolEvent("validate", "pytest retry", { ok: true }),
    ])).toBe("done");
    expect(activityStateLabel(failed, "working")).toBe("working");
    expect(activityStateLabel(completed, "interrupted")).toBe("interrupted");
    expect(activityStateLabel(
      [toolEvent("read", "read api.ts", { output: null, ok: null })],
      "interrupted",
    )).toBe("interrupted");
    expect(activityStateLabel(failed, "waiting-for-you")).toBe("waiting for you");
    expect(activityStateLabel(completed, "waiting-for-you")).toBe("waiting for you");
    expect(
      activityStateLabel([toolEvent("ask", "ask henry", { output: null, ok: null })]),
    ).toBe("waiting for you");
  });

  test("maps active, approval, pending, and terminal provider states", () => {
    expect(activityRunStateFromProvider("working", 0, false)).toBe("working");
    expect(activityRunStateFromProvider("working", 1, true)).toBe("waiting-for-you");
    expect(activityRunStateFromProvider("waiting-approval", 0, false)).toBe("waiting-for-you");
    expect(activityRunStateFromProvider("idle", 1, false)).toBe("waiting-for-you");
    expect(activityRunStateFromProvider("dead", 0, true)).toBe("failed");
    expect(activityRunStateFromProvider("dead", 1, true)).toBe("failed");
    expect(activityRunStateFromProvider("interrupted", 0, false)).toBe("interrupted");
    expect(activityRunStateFromProvider("interrupted", 1, true)).toBe("interrupted");
    expect(activityRunStateFromProvider("error", 0, false)).toBe("failed");
    expect(activityRunStateFromProvider("blocked", 0, false)).toBe("failed");
    expect(activityRunStateFromProvider("completed", 0, true)).toBe("idle");
  });
});
