import { describe, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import {
  activityCountsLabel,
  activityElapsedLabel,
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
    expect(activityStateLabel(completed, "working")).toBe("working");
    expect(activityStateLabel(completed)).toBe("done");
    expect(activityStateLabel([toolEvent("read", "read api.ts", { output: null, ok: null })])).toBe("working");
    expect(activityStateLabel([toolEvent("validate", "pytest", { ok: false })])).toBe("failed");
    expect(activityStateLabel(completed, "waiting-for-you")).toBe("waiting for you");
    expect(
      activityStateLabel([toolEvent("ask", "ask henry", { output: null, ok: null })]),
    ).toBe("waiting for you");
  });

  test("does not invent meaning for an unsafe unknown archetype", () => {
    const events = [toolEvent("deploy-production-and-delete-data", "deployment completed")];
    expect(activitySemanticSummary(events)).toBeNull();
    expect(activityCountsLabel(events)).toBe("1 tool call");
  });

  test("keeps elapsed time as secondary metadata", () => {
    expect(activityElapsedLabel([
      toolEvent("read", "read api.ts", {}, "2026-08-02T12:00:00.000Z"),
      thinkingEvent("done", "2026-08-02T12:01:04.000Z"),
    ])).toBe("1m 4s");
  });
});
