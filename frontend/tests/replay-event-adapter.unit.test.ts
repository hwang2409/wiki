import { describe, expect, test } from "vitest";

import type {
  ProviderStreamEvent,
  ReplayRawEvent,
  ReplayTimelineEvent,
} from "../src/api";
import {
  providerEventPayload,
  providerEventToBlocks,
  type PresentationBlock,
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

function providerEvent(payload: Record<string, unknown>): ProviderStreamEvent {
  return {
    seq: 1,
    raw_seq: 1,
    normalized_at: "2026-08-25T12:00:00Z",
    disposition: "rendered",
    kind: "item_completed",
    lifecycle_state: null,
    payload,
  };
}

function replayEvent(payload: Record<string, unknown>): ReplayRawEvent {
  return {
    run_id: "run-1",
    seq: 1,
    raw: { payload },
  };
}

function toolBlock(blocks: PresentationBlock[]): PresentationBlock & { type: "tool" } {
  const block = blocks.find((candidate) => candidate.type === "tool");
  expect(block?.type).toBe("tool");
  return block as PresentationBlock & { type: "tool" };
}

describe("provider event adapter", () => {
  test("reads the same payload shape from transcript and replay events", () => {
    const payload = { message: "model changed" };
    expect(providerEventPayload(providerEvent(payload))).toEqual(payload);
    expect(providerEventPayload(replayEvent(payload))).toEqual(payload);
  });

  test.each([
    {
      label: "completed mcp tool",
      payload: {
        method: "item/completed",
        params: {
          item: {
            type: "mcpToolCall",
            server: "filesystem",
            tool: "read_file",
            status: "completed",
            result: { isError: false },
          },
        },
      },
      expected: true,
    },
    {
      label: "completed web search",
      payload: {
        method: "item/completed",
        params: { item: { type: "webSearch", query: "wiki", status: "completed" } },
      },
      expected: true,
    },
    {
      label: "completed dynamic tool",
      payload: {
        method: "item/completed",
        params: { item: { type: "dynamicToolCall", tool: "lookup", status: "completed" } },
      },
      expected: true,
    },
    {
      label: "completed collaboration tool",
      payload: {
        method: "item/completed",
        params: { item: { type: "collabAgentToolCall", action: "review", status: "completed" } },
      },
      expected: true,
    },
    {
      label: "completed tool error",
      payload: {
        method: "item/completed",
        params: {
          item: {
            type: "mcpToolCall",
            server: "filesystem",
            tool: "read_file",
            status: "completed",
            result: { isError: true },
          },
        },
      },
      expected: false,
    },
    {
      label: "in-progress tool",
      payload: {
        method: "item/started",
        params: { item: { type: "dynamicToolCall", tool: "lookup", status: "in_progress" } },
      },
      expected: null,
    },
  ])("uses terminal status for the same $label fixture on both paths", ({ payload, expected }) => {
    const event = timelineEvent({ summary: "tool event" });
    const transcriptBlocks = providerEventToBlocks(providerEvent(payload), event);
    const replayBlocks = providerEventToBlocks(replayEvent(payload), event);

    expect(replayBlocks).toEqual(transcriptBlocks);
    expect(toolBlock(transcriptBlocks).tool.ok).toBe(expected);
  });

  test("keeps mixed Claude text and tools in provider order on both paths", () => {
    const payload = {
      message: {
        role: "assistant",
        content: [
          { type: "text", text: "I will inspect the file." },
          { type: "tool_use", name: "Read", input: { file_path: "README.md" } },
          { type: "text", text: "Then I will report back." },
        ],
      },
    };
    const event = timelineEvent({ kind: "claude_assistant", summary: "text and tool" });
    const transcriptBlocks = providerEventToBlocks(providerEvent(payload), event);
    const replayBlocks = providerEventToBlocks(replayEvent(payload), event);

    expect(replayBlocks).toEqual(transcriptBlocks);
    expect(transcriptBlocks.map((block) => block.type)).toEqual(["message", "tool", "message"]);
  });

  test("marks a Claude result with nested result.isError as failed on both paths", () => {
    const payload = {
      message: {
        role: "user",
        content: [{
          type: "tool_result",
          content: [{ type: "text", text: "permission denied" }],
          result: { isError: true },
        }],
      },
    };
    const event = timelineEvent({ kind: "claude_user", summary: "tool result" });
    const transcriptBlocks = providerEventToBlocks(providerEvent(payload), event);
    const replayBlocks = providerEventToBlocks(replayEvent(payload), event);

    expect(replayBlocks).toEqual(transcriptBlocks);
    expect(toolBlock(transcriptBlocks).tool.ok).toBe(false);
  });

  test("keeps an unknown provider item visible as the same marker on both paths", () => {
    const payload = { params: { item: { type: "futureProviderItem" } } };
    const event = timelineEvent({ kind: "future_kind", summary: "mystery item" });
    const transcriptBlocks = providerEventToBlocks(providerEvent(payload), event);
    const replayBlocks = providerEventToBlocks(replayEvent(payload), event);

    expect(replayBlocks).toEqual(transcriptBlocks);
    expect(transcriptBlocks).toEqual([{ type: "marker", text: "event · mystery item" }]);
  });
});
