import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";

import type {
  ProviderStreamEvent,
  ReplayRawEvent,
  ReplayTimelineEvent,
} from "../src/api";
import {
  providerEventPayload,
  providerEventPresentation,
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

  test.each([
    {
      label: "web search",
      item: { type: "webSearch", id: "search-1", query: "wiki" },
      name: "webSearch",
      target: "wiki",
    },
    {
      label: "dynamic tool",
      item: {
        type: "dynamicToolCall",
        id: "dynamic-1",
        tool: "lookup",
        contentItems: [{ type: "text", text: "needle" }],
      },
      name: "lookup",
      target: "needle",
    },
    {
      label: "collaboration tool",
      item: { type: "collabAgentToolCall", id: "collab-1", action: "review" },
      name: "collabAgentToolCall",
      target: "review",
    },
  ])("renders the real Codex $label item as a tool", ({ item, name, target }) => {
    const presentation = replayEventPresentation(
      timelineEvent({ kind: "item_completed", summary: `completed ${item.type}` }),
      rawEvent({ method: "item/completed", params: { item } }),
    );
    expect(presentation.category).toBe("activity");
    expect(presentation.blocks).toHaveLength(1);
    expect(presentation.blocks[0]).toMatchObject({
      type: "tool",
      tool: { name, summary: expect.stringContaining(target) },
    });
  });

  test("keeps Claude text and tool blocks in their provider order", () => {
    const presentation = replayEventPresentation(
      timelineEvent({ kind: "claude_assistant", summary: "text and tool" }),
      rawEvent({
        type: "assistant",
        message: {
          role: "assistant",
          content: [
            { type: "text", text: "I will inspect the file." },
            {
              type: "tool_use",
              id: "toolu_fixture",
              name: "Read",
              input: { file_path: "README.md" },
            },
            { type: "text", text: "Then I will report back." },
          ],
        },
      }),
    );
    expect(presentation.category).toBe("message");
    expect(presentation.blocks).toEqual([
      { type: "message", role: "assistant", text: "I will inspect the file." },
      {
        type: "tool",
        tool: expect.objectContaining({ name: "Read", summary: "Read README.md" }),
      },
      { type: "message", role: "assistant", text: "Then I will report back." },
    ]);
  });

  test("uses the shared adapter for transcript row classification", () => {
    const source = readFileSync(resolve(process.cwd(), "src/session.tsx"), "utf8");
    expect(source).toContain("providerEventPresentation(row.event).category");

    const event = timelineEvent();
    const normalized = {
      id: event.seq,
      kind: "tool" as const,
      ts: event.ts,
      text: "",
      disposition: "rendered" as const,
      tool: {
        name: "Read",
        input: "README.md",
        output: null,
        ok: true,
        archetype: "read",
        summary: "Read README.md",
      },
    };
    expect(providerEventPresentation(normalized).category).toBe("activity");
  });

  test("labels an unknown provider item instead of hiding it", () => {
    const presentation = replayEventPresentation(
      timelineEvent({ kind: "item_completed", summary: "mystery item" }),
      rawEvent({ params: { item: { type: "futureProviderItem" } } }),
    );
    expect(presentation.category).toBe("marker");
    expect(presentation.blocks).toEqual([{ type: "marker", text: "event · mystery item" }]);
  });
});
