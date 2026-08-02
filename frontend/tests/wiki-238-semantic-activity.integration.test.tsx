import { describe, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import {
  activityCountsLabel,
  activityElapsedLabel,
  activityRunStateFromProvider,
  activitySemanticSummary,
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

function thinkingEvent(text = "considering the evidence", ts = "2026-08-02T12:00:02.000Z"): SessionEvent {
  return {
    id: 2,
    kind: "thinking",
    ts,
    text,
    disposition: "rendered",
  };
}

describe("semantic model activity groups", () => {
  test("maps supported tools and reasoning to safe summaries", () => {
    expect(activitySemanticSummary([toolEvent("read", "read files_test.py")])).toBe("reading files_test.py");
    expect(activitySemanticSummary([toolEvent("validate", "pytest frontend/tests")])).toBe("running tests");
    expect(activitySemanticSummary([toolEvent("git", "git diff")])).toBe("checking the final diff");
    expect(activitySemanticSummary([toolEvent("read", "read api.ts"), thinkingEvent()])).toBe("reasoning through the task");
  });

  test("falls back to the existing count label", () => {
    const events = [toolEvent("tool", "opaque provider action")];
    expect(activitySemanticSummary(events)).toBeNull();
    expect(activityCountsLabel(events)).toBe("1 tool call");
  });

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
    expect(activityRunStateFromProvider("error", 0, false)).toBe("failed");
    expect(activityRunStateFromProvider("blocked", 0, false)).toBe("failed");
    expect(activityRunStateFromProvider("completed", 0, true)).toBe("idle");
  });

  test("does not invent meaning for an unsafe unknown archetype", () => {
    const events = [toolEvent("deploy-production-and-delete-data", "deployment completed")];
    expect(activitySemanticSummary(events)).toBeNull();
    expect(activityCountsLabel(events)).toBe("1 tool call");
  });

  test("keeps elapsed time as secondary metadata", () => {
    expect(activityElapsedLabel([
      toolEvent(
        "read",
        "read api.ts",
        { completed_at: "2026-08-02T12:00:03.000Z" },
        "2026-08-02T12:00:00.000Z",
      ),
      thinkingEvent("still working", "2026-08-02T12:00:01.000Z"),
    ])).toBe("3s");
  });
});
