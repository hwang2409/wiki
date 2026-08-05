import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import {
  commandExecutionCards,
  CodexStreamHighlights,
  deriveHookChips,
  groupProviderDiagnostics,
  matchedTerminalInteractions,
  parseDiffSnapshot,
} from "../src/codex-stream-renderers";
import type { ProviderStreamEvent } from "../src/api";

afterEach(cleanup);

// WIKI-244: the working diff renders open — no section toggle exists. The
// helper now asserts that no disclosure control gates the diff.
function expandDiffSection() {
  expect(screen.queryByRole("button", { name: /working diff/ })).toBeNull();
}

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

  it("renders nested moderation category flags on the diagnostic row", () => {
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
    expect(document.querySelector(".codex-stream-diagnostic-flag")?.textContent).toBe("blocked, violence");
  });

  it("uses the newest complete diff snapshot and drops reverted files", () => {
    const first = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    const second = "diff --git a/b.txt b/b.txt\n--- a/b.txt\n+++ b/b.txt\n@@ -1,1 +1,1 @@\n-x\n+y";
    render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: first }), event("turn_diff_updated", 2, { diff: second })]} />);
    expandDiffSection();
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
    expandDiffSection();
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
    expandDiffSection();
    expect(view.container.textContent).toContain("current.txt");
  });

  it("parses a diff snapshot into files", () => {
    const source = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    expect(parseDiffSnapshot(source)).toStrictEqual(parseDiffSnapshot(source));
  });

  it("renders large files open with a bounded preview", () => {
    const lines = Array.from({ length: 200 }, (_, index) => `+${"x".repeat(1_000)}-${index}`).join("\n");
    const source = `diff --git a/large.txt b/large.txt\n--- a/large.txt\n+++ b/large.txt\n@@ -0,0 +1,200 @@\n${lines}`;
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expandDiffSection();
    // WIKI-244: large files render open and bounded — no per-file toggle.
    expect(view.queryByRole("button", { name: /large\.txt/ })).toBeNull();
    const renderedLines = view.container.querySelectorAll(".codex-stream-diff-line");
    expect(renderedLines.length).toBeGreaterThan(0);
    expect(renderedLines.length).toBeLessThan(200);
  });

  it("counts hunk headers against diff row and byte limits", () => {
    const hunks = Array.from({ length: 3_000 }, (_, index) => `@@ -${index + 1},0 +${index + 1},0 @@ ${"header".repeat(8)}`).join("\n");
    const source = `diff --git a/headers.txt b/headers.txt\n--- a/headers.txt\n+++ b/headers.txt\n${hunks}`;
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expandDiffSection();
    expect(view.queryByRole("button", { name: /headers\.txt/ })).toBeNull();
    const renderedHeaders = view.container.querySelectorAll(".codex-stream-diff-hunk-head");
    expect(renderedHeaders.length).toBeLessThan(3_000);
    expect(renderedHeaders.length).toBeLessThanOrEqual(2_000);
  });

  it("caps diff source bytes and file count before rendering", () => {
    const source = Array.from({ length: 120 }, (_, index) => (
      `diff --git a/file-${index}.txt b/file-${index}.txt\n--- a/file-${index}.txt\n+++ b/file-${index}.txt\n@@ -0,1 +0,1 @@\n+${"x".repeat(6_000)}`
    )).join("\n");
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expandDiffSection();
    expect(view.container.querySelectorAll(".codex-stream-diff-file").length).toBeLessThanOrEqual(100);
    expect(view.getByTestId("codex-diff-omitted")).toBeTruthy();
  });

  it("groups clamp warnings by stable source, kind, and code — never by display text", () => {
    const first = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /Users/henry/.codex/plugins/cache/openai-codex/codex/1.0.6/hooks/hooks.json",
    });
    const second = event("warning", 2, {
      message: "clamping UserPromptSubmit hook timeout to 3s in /Users/henry/.codex/plugins/other/plugin.json",
    });
    const groups = groupProviderDiagnostics([first, second]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.source).toBe("codex");
    expect(groups[0]?.kind).toBe("clamp");
    expect(groups[0]?.code).toBe("hook-timeout");
    expect(groups[0]?.severity).toBe("advisory");
    expect(groups[0]?.actionRequired).toBe(false);
    expect(groups[0]?.events.map((e) => e.seq)).toEqual([1, 2]);
  });

  it("keeps distinct diagnostic sources as separate rows", () => {
    const clamp = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /path/hooks.json",
    });
    const moderation = event("turn_moderationMetadata_warning", 2, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["violence"] }],
      }] } } },
    });
    const groups = groupProviderDiagnostics([clamp, moderation]);
    expect(groups).toHaveLength(2);
    const sources = groups.map((g) => g.source).sort();
    expect(sources).toEqual(["codex", "moderation"]);
  });

  it("promotes group severity to the highest per-event severity", () => {
    const flagged = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: false,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const blocked = event("turn_moderationMetadata_warning", 2, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const groups = groupProviderDiagnostics([flagged, blocked]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.severity).toBe("danger");
    expect(groups[0]?.actionRequired).toBe(true);
    expect(groups[0]?.summary).toBe("moderation blocked");
    expect(groups[0]?.label).toBe("blocked");
    expect(groups[0]?.events).toHaveLength(2);
  });

  it("keeps danger severity when a lower-severity event arrives after the escalation", () => {
    const blocked = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const flaggedAgain = event("turn_moderationMetadata_warning", 2, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: false,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const groups = groupProviderDiagnostics([blocked, flaggedAgain]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.severity).toBe("danger");
    expect(groups[0]?.summary).toBe("moderation blocked");
    expect(groups[0]?.label).toBe("blocked");
    expect(groups[0]?.actionRequired).toBe(true);
  });

  it("splits same source/kind into separate rows when the code differs", () => {
    const hookTimeout = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json",
    });
    const otherClamp = event("warning", 2, {
      message: "clamping tool concurrency to 4",
    });
    const groups = groupProviderDiagnostics([hookTimeout, otherClamp]);
    expect(groups).toHaveLength(2);
    const codes = groups.map((group) => group.code).sort();
    expect(codes).toEqual(["generic", "hook-timeout"]);
    const kinds = groups.map((group) => group.kind);
    expect(kinds.every((kind) => kind === "clamp")).toBe(true);
  });

  it("canonicalizes moderation flag order so equivalent flag sets share one row", () => {
    const forwards = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: false,
        results: [{ labels: ["hate", "violence"] }],
      }] } } },
    });
    const reversed = event("turn_moderationMetadata_warning", 2, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: false,
        results: [{ labels: ["violence", "hate"] }],
      }] } } },
    });
    const groups = groupProviderDiagnostics([forwards, reversed]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.events).toHaveLength(2);
    expect(groups[0]?.code).toBe("hate+violence");
  });

  it("classifies non-clamp codex warnings as warning-severity runtime notices", () => {
    const stray = event("warning", 1, { message: "provider disconnected: retrying" });
    const groups = groupProviderDiagnostics([stray]);
    expect(groups).toHaveLength(1);
    expect(groups[0]?.source).toBe("codex");
    expect(groups[0]?.kind).toBe("unknown");
    expect(groups[0]?.severity).toBe("warning");
    expect(groups[0]?.summary).toBe("runtime warning");
    expect(groups[0]?.label).toBe("warning");
    expect(groups[0]?.actionRequired).toBe(false);
  });

  it("auto-opens on the false->true escalation and then respects a manual close", () => {
    const advisory = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: false,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const view = render(<CodexStreamHighlights events={[advisory]} />);
    const beforeButton = () => view.container.querySelector("button.codex-stream-diagnostic-head")!;
    expect(beforeButton().getAttribute("aria-expanded")).toBe("false");

    const firstBlocked = event("turn_moderationMetadata_warning", 2, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    view.rerender(<CodexStreamHighlights events={[advisory, firstBlocked]} />);
    expect(beforeButton().getAttribute("aria-expanded")).toBe("true");
    expect(view.container.querySelector("[data-testid='codex-diagnostic-body']")).toBeTruthy();

    // User manually collapses the row.
    fireEvent.click(beforeButton());
    expect(beforeButton().getAttribute("aria-expanded")).toBe("false");

    // Another blocked event arrives — must NOT reopen. Repeated diagnostics
    // cannot overwrite an explicit dismissal.
    const secondBlocked = event("turn_moderationMetadata_warning", 3, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    view.rerender(<CodexStreamHighlights events={[advisory, firstBlocked, secondBlocked]} />);
    expect(beforeButton().getAttribute("aria-expanded")).toBe("false");

    // Rerendering identical props also must not reopen (no effect loops).
    view.rerender(<CodexStreamHighlights events={[advisory, firstBlocked, secondBlocked]} />);
    expect(beforeButton().getAttribute("aria-expanded")).toBe("false");
  });

  it("returns an empty group list and renders nothing for zero diagnostic events", () => {
    expect(groupProviderDiagnostics([])).toEqual([]);
    const view = render(<CodexStreamHighlights events={[]} />);
    expect(view.container.querySelector("[data-testid='codex-diagnostics']")).toBeNull();
    expect(view.container.querySelector(".codex-stream-diagnostic")).toBeNull();
  });

  it("preserves every raw warning message in the expanded diagnostics body", () => {
    const first = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json",
    });
    const second = event("warning", 2, {
      message: "clamping UserPromptSubmit hook timeout to 3s in /b/plugin.json",
    });
    render(<CodexStreamHighlights events={[first, second]} />);
    const toggle = screen.getByRole("button", { name: /hook timeout clamped/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    const body = document.querySelector("[data-testid='codex-diagnostic-body']");
    expect(body).toBeTruthy();
    expect(body!.textContent).toContain("/a/hooks.json");
    expect(body!.textContent).toContain("/b/plugin.json");
  });

  it("auto-opens groups when user action is required", () => {
    const blocked = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    render(<CodexStreamHighlights events={[blocked]} />);
    const toggle = screen.getByRole("button", { name: /moderation blocked/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("keeps advisory clamp groups collapsed on first render", () => {
    const clamp = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json",
    });
    render(<CodexStreamHighlights events={[clamp]} />);
    const toggle = screen.getByRole("button", { name: /hook timeout clamped/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("renders clamp notices with advisory styling — no danger class", () => {
    const clamp = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json",
    });
    const { container } = render(<CodexStreamHighlights events={[clamp]} />);
    const row = container.querySelector(".codex-stream-diagnostic");
    expect(row).toBeTruthy();
    expect(row!.className).toContain("is-advisory");
    expect(row!.className).not.toContain("is-danger");
  });

  it("reserves danger styling for blocked runs", () => {
    const blocked = event("turn_moderationMetadata_warning", 1, {
      metadata: { prompt: { omnimod: { outputs: [{
        is_blocked: true,
        results: [{ labels: ["hate"] }],
      }] } } },
    });
    const { container } = render(<CodexStreamHighlights events={[blocked]} />);
    const row = container.querySelector(".codex-stream-diagnostic");
    expect(row).toBeTruthy();
    expect(row!.className).toContain("is-danger");
  });

  it("shows a coalesced count when a group has more than one event", () => {
    const events = [
      event("warning", 1, { message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json" }),
      event("warning", 2, { message: "clamping SessionEnd hook timeout to 3s in /b/hooks.json" }),
      event("warning", 3, { message: "clamping SessionEnd hook timeout to 3s in /c/hooks.json" }),
    ];
    render(<CodexStreamHighlights events={events} />);
    const toggle = screen.getByRole("button", { name: /hook timeout clamped/i });
    expect(within(toggle).getByText("3")).toBeTruthy();
  });

  it("hides the count badge when a group has exactly one event", () => {
    const clamp = event("warning", 1, {
      message: "clamping SessionEnd hook timeout to 3s in /a/hooks.json",
    });
    const { container } = render(<CodexStreamHighlights events={[clamp]} />);
    expect(container.querySelector(".codex-stream-diagnostic-count")).toBeNull();
  });

  it("renders the working diff open with no disclosure control (WIKI-244)", () => {
    const source = "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-old\n+new";
    const view = render(<CodexStreamHighlights events={[event("turn_diff_updated", 1, { diff: source })]} />);
    expect(view.queryByRole("button", { name: /working diff/ })).toBeNull();
    expect(view.getByText("a.txt")).toBeTruthy();
    expect(view.container.querySelector(".codex-stream-diff-body")?.textContent).toContain("+new");
    expect(view.container.querySelector("[data-testid='codex-diff-omitted']")).toBeNull();
    expect(view.getByRole("button", { name: /copy raw diff/ })).toBeTruthy();
  });
});
