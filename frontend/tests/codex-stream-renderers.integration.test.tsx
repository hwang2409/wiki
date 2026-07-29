import { describe, expect, it } from "vitest";
import {
  applyDiffSnapshot,
  deriveHookChips,
  matchedTerminalInteractions,
} from "../src/codex-stream-renderers";
import type { ProviderStreamEvent } from "../src/api";

function event(
  kind: string,
  seq: number,
  params: Record<string, unknown>,
): ProviderStreamEvent {
  return {
    seq,
    raw_seq: seq,
    normalized_at: `2026-07-29T12:00:0${seq}Z`,
    disposition: kind.startsWith("hook_") ? "summarized" : "rendered",
    kind,
    lifecycle_state: null,
    payload: { method: kind, params },
  };
}

describe("codex stream renderers", () => {
  it("pairs hook start and complete by hook name and turn id", () => {
    const events = [
      event("hook_started", 1, {
        turnId: "turn-1",
        run: { eventName: "userPromptSubmit", id: "hook-1" },
      }),
      event("hook_completed", 2, {
        turnId: "turn-1",
        run: { eventName: "userPromptSubmit", durationMs: 52 },
      }),
    ];
    expect(deriveHookChips(events)).toEqual([
      { key: "userPromptSubmit\u0000turn-1\u00002", name: "userPromptSubmit", durationMs: 52, seq: 2 },
    ]);
  });

  it("does not render terminal interaction without its command item", () => {
    const orphan = event("item_commandExecution_terminalInteraction", 1, {
      itemId: "exec-1",
      stdin: "yes",
    });
    expect(matchedTerminalInteractions([orphan])).toEqual([]);
    const command = event("item_started", 2, {
      item: { id: "exec-1", type: "commandExecution" },
    });
    expect(matchedTerminalInteractions([command, orphan])).toHaveLength(1);
  });

  it("keeps rolling diff files keyed by path across snapshots", () => {
    const first = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    const second = "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-x\n+y";
    const files = applyDiffSnapshot(applyDiffSnapshot(new Map(), first), second);
    expect([...files.keys()]).toEqual(["a.txt", "b.txt"]);
  });
});
