import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import {
  commandExecutionCards,
  CodexStreamHighlights,
  deriveHookChips,
  matchedTerminalInteractions,
  parseDiffSnapshot,
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

  it("does not show a dangling hook chip for a started-only hook", () => {
    const started = event("hook_started", 1, {
      turnId: "turn-1",
      run: { eventName: "userPromptSubmit", id: "hook-1" },
    });
    expect(deriveHookChips([started])).toEqual([]);
    render(<CodexStreamHighlights events={[started]} />);
    expect(screen.queryByTestId("codex-hook-chips")).toBeNull();
  });

  it("renders a hook chip only after the matching completion event", () => {
    const started = event("hook_started", 1, {
      turnId: "turn-1",
      run: { eventName: "userPromptSubmit", id: "hook-1" },
    });
    const completed = event("hook_completed", 2, {
      turnId: "turn-1",
      run: { eventName: "userPromptSubmit", durationMs: 52 },
    });
    render(<CodexStreamHighlights events={[started, completed]} />);
    expect(screen.getByTestId("codex-hook-chips")).toBeTruthy();
    expect(screen.getByText("hook: userPromptSubmit")).toBeTruthy();
  });

  it("attaches terminal interaction to one command card and keeps empty stdin visible", () => {
    const orphan = event("item_commandExecution_terminalInteraction", 1, {
      itemId: "exec-1",
      stdin: "",
    });
    expect(matchedTerminalInteractions([orphan])).toHaveLength(1);
    const fallbackCards = commandExecutionCards([orphan]);
    expect(fallbackCards).toHaveLength(1);
    expect(fallbackCards[0]?.command).toBeNull();
    const fallbackRender = render(<CodexStreamHighlights events={[orphan]} />);
    expect(fallbackRender.container.querySelectorAll(".codex-stream-command-card")).toHaveLength(1);
    expect(fallbackRender.getByText("exec-1")).toBeTruthy();
    fallbackRender.unmount();
    const command = event("item_started", 2, {
      item: { id: "exec-1", type: "commandExecution", command: "read prompt" },
    });
    expect(matchedTerminalInteractions([command, orphan])).toHaveLength(1);
    const cards = commandExecutionCards([command, orphan]);
    expect(cards).toHaveLength(1);
    expect(cards[0]?.interactions).toHaveLength(1);
    expect(cards[0]?.interactions[0]?.stdin).toBe("");
    render(<CodexStreamHighlights events={[command, orphan]} />);
    expect(document.querySelectorAll(".codex-stream-command-card")).toHaveLength(1);
    expect(screen.getByText("empty stdin")).toBeTruthy();
  });

  it("renders the real empty skills-changed payload as a visible refresh chip", () => {
    render(<CodexStreamHighlights events={[event("skills_changed", 1, {})]} />);
    expect(screen.getByText("skills updated")).toBeTruthy();
    expect(screen.getByText("list refreshed")).toBeTruthy();
  });

  it("renders nested moderation category flags in the warning chip", () => {
    render(
      <CodexStreamHighlights
        events={[event("turn_moderationMetadata_warning", 1, {
          metadata: { prompt: { omnimod: { outputs: [{
            is_blocked: true,
            results: [{ labels: ["violence"] }],
          }] } } },
        })]}
      />,
    );
    expect(document.querySelector(".codex-stream-warning-flag")?.textContent).toBe("blocked, violence");
  });

  it("uses the newest complete diff snapshot and drops reverted files", () => {
    const first = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    const second = "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-x\n+y";
    render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: first }), event("turn_diff_updated", 2, { diff: second })]} />);
    expect(screen.getByText("b.txt")).toBeTruthy();
    expect(screen.queryByText("a.txt")).toBeNull();
    expect(document.querySelector(".codex-stream-diff-body")?.textContent).toContain("+y");
    expect(document.querySelector(".codex-stream-diff-body")?.textContent).not.toContain("+new");
  });

  it("memoizes parsed diff snapshots by content", () => {
    const source = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    expect(parseDiffSnapshot(source)).toBe(parseDiffSnapshot(source));
  });
});
