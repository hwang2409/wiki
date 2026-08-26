import { describe, expect, test } from "vitest";

import type {
  ProviderStreamEvent,
  ReplayRawEvent,
  ReplayTimelineEvent,
} from "../src/api";
import {
  providerEventPayload,
  replayEventPresentation,
} from "../src/replay-event-adapter";

function timelineEvent(overrides: Partial<ReplayTimelineEvent> = {}): ReplayTimelineEvent {
  return {
    seq: 1,
    raw_seq: 1,
    ts: "2026-08-25T12:00:00Z",
    kind: "item_completed",
    disposition: "rendered",
    lifecycle_state: null,
    summary: "event",
    bookmark: null,
    ...overrides,
  };
}

function rawEvent(payload: Record<string, unknown>): ReplayRawEvent {
  return {
    run_id: "run-1",
    seq: 1,
    raw: { payload },
  };
}

describe("replay event adapter", () => {
  test("reads the same payload shape from transcript and replay events", () => {
    const payload = { message: "model changed" };
    const normalized: ProviderStreamEvent = {
      seq: 1,
      raw_seq: 1,
      normalized_at: "2026-08-25T12:00:00Z",
      disposition: "rendered",
      kind: "model_changed",
      lifecycle_state: null,
      payload,
    };

    expect(providerEventPayload(normalized)).toEqual(payload);
    expect(providerEventPayload(rawEvent(payload))).toEqual(payload);
  });

  test("adapts codex messages and commands to transcript presenters", () => {
    const message = replayEventPresentation(
      timelineEvent({ kind: "item_completed", summary: "user message" }),
      rawEvent({
        params: {
          item: {
            type: "userMessage",
            content: [{ type: "text", text: "Review the replay" }],
          },
        },
      }),
    );
    expect(message.message).toBe(true);
    expect(message.messageRole).toBe("user");
    expect(message.messageText).toBe("Review the replay");
    expect(message.tool).toBeNull();

    const command = replayEventPresentation(
      timelineEvent({ summary: "codex item/completed: commandExecution" }),
      rawEvent({
        params: {
          item: {
            type: "commandExecution",
            command: "npm test",
            exitCode: 0,
          },
        },
      }),
    );
    expect(command.message).toBe(false);
    expect(command.tool).toMatchObject({
      name: "Bash",
      input: "npm test",
      ok: true,
      archetype: "bash",
      summary: "bash npm test",
    });
  });

  test("adapts claude tool blocks and errors", () => {
    const presentation = replayEventPresentation(
      timelineEvent({ kind: "claude_assistant", summary: "tool_use" }),
      rawEvent({
        message: {
          role: "assistant",
          content: [{
            type: "tool_use",
            name: "Read",
            input: { file_path: "README.md" },
          }],
        },
      }),
    );
    expect(presentation.tool).toMatchObject({
      name: "Read",
      archetype: "read",
      summary: "Read README.md",
    });
    expect(presentation.message).toBe(false);
  });
});
