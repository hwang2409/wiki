import { fireEvent, render, screen } from "@testing-library/react";
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

  it("pairs hooks by run id without a turn id and renders completion-only hooks", () => {
    const paired = deriveHookChips([
      event("hook_started", 1, { run: { eventName: "sessionStart", id: "hook-1" } }),
      event("hook_completed", 2, { run: { eventName: "sessionStart", id: "hook-1", durationMs: 18 } }),
    ]);
    expect(paired[0]?.name).toBe("sessionStart");
    expect(paired[0]?.durationMs).toBe(18);

    const completionOnly = deriveHookChips([
      event("hook_completed", 3, { run: { eventName: "sessionEnd", durationMs: 24 } }),
    ]);
    expect(completionOnly).toEqual([
      { key: "name:sessionEnd\u00003", name: "sessionEnd", durationMs: 24, seq: 3 },
    ]);
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

  it("uses the backend-pinned current-turn diff and honors a reset", () => {
    const oldDiff = "diff --git a/old.txt b/old.txt\n--- a/old.txt\n+++ b/old.txt\n@@ -1,1 +1,1 @@\n-old\n+old";
    const currentDiff = "diff --git a/current.txt b/current.txt\n--- a/current.txt\n+++ b/current.txt\n@@ -1,1 +1,1 @@\n-old\n+current";
    const view = render(
      <CodexStreamHighlights
        events={[event("turn_diff_updated", 1, { diff: oldDiff })]}
        currentTurnDiff={currentDiff}
      />,
    );
    expect(view.getByText("current.txt")).toBeTruthy();
    expect(view.queryByText("old.txt")).toBeNull();
    view.unmount();

    const reset = render(
      <CodexStreamHighlights
        events={[event("turn_diff_updated", 1, { diff: oldDiff })]}
        currentTurnDiff={null}
      />,
    );
    expect(reset.container.querySelector("[data-testid='codex-diff-renderer']")).toBeNull();
  });

  it("renders a pinned diff when the event window contains only summarized events", () => {
    const summarized = event("thread_tokenUsage_updated", 1, {});
    summarized.disposition = "summarized";
    const currentDiff = "diff --git a/current.txt b/current.txt\n--- a/current.txt\n+++ b/current.txt\n@@ -1,1 +1,1 @@\n-old\n+current";
    const view = render(
      <CodexStreamHighlights events={[summarized]} currentTurnDiff={currentDiff} />,
    );
    expect(view.container.querySelector("[data-testid='codex-diff-renderer']")).toBeTruthy();
    expect(view.container.textContent).toContain("current.txt");
  });

  it("parses a diff snapshot into files", () => {
    const source = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    expect(parseDiffSnapshot(source)).toStrictEqual(parseDiffSnapshot(source));
  });

  it("collapses large files and limits the expanded preview", () => {
    const lines = Array.from({ length: 200 }, (_, index) => `+${"x".repeat(1_000)}-${index}`).join("\n");
    const source = `diff --git a/large.txt b/large.txt\n--- a/large.txt\n+++ b/large.txt\n@@ -0,0 +1,200 @@\n${lines}`;
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expect(view.container.querySelector(".codex-stream-diff-body")).toBeNull();
    fireEvent.click(view.getByRole("button", { name: /large\.txt/ }));
    const renderedLines = view.container.querySelectorAll(".codex-stream-diff-line");
    expect(renderedLines.length).toBeGreaterThan(0);
    expect(renderedLines.length).toBeLessThan(200);
  });

  it("counts hunk headers against diff row and byte limits", () => {
    const hunks = Array.from({ length: 3_000 }, (_, index) => `@@ -${index + 1},0 +${index + 1},0 @@ ${"header".repeat(8)}`).join("\n");
    const source = `diff --git a/headers.txt b/headers.txt\n--- a/headers.txt\n+++ b/headers.txt\n${hunks}`;
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    fireEvent.click(view.getByRole("button", { name: /headers\.txt/ }));
    const renderedHeaders = view.container.querySelectorAll(".codex-stream-diff-hunk-head");
    expect(renderedHeaders.length).toBeLessThan(3_000);
    expect(renderedHeaders.length).toBeLessThanOrEqual(2_000);
  });

  it("caps diff source bytes and file count before rendering", () => {
    const source = Array.from({ length: 120 }, (_, index) => (
      `diff --git a/file-${index}.txt b/file-${index}.txt\n--- a/file-${index}.txt\n+++ b/file-${index}.txt\n@@ -0,1 +0,1 @@\n+${"x".repeat(6_000)}`
    )).join("\n");
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expect(view.container.querySelectorAll(".codex-stream-diff-file").length).toBeLessThanOrEqual(100);
    expect(view.getByTestId("codex-diff-omitted")).toBeTruthy();
  });
});
