// @vitest-environment jsdom
import { useEffect, useRef } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

import type { SessionEvent } from "../src/api";
import { ArtifactBlock } from "../src/artifact-block";
import {
  ArtifactInspector,
  inspectorTitle,
  isTextEntryTarget,
  useArtifactInspector,
} from "../src/artifact-inspector";
import { SessionSidebar, InspectableSessionTab } from "../src/session";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver ??= NoopObserver;
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopObserver;
});

function artifactEvent(overrides: Partial<SessionEvent> & { artifact_id: string }): SessionEvent {
  return {
    id: 1,
    kind: "artifact",
    ts: null,
    text: "",
    disposition: "rendered",
    ...overrides,
  };
}

const jsonEvent = artifactEvent({
  artifact_id: "json-1",
  title: "Run summary",
  artifact: { kind: "json", json_data: { ok: true } },
});

const codeEvent = artifactEvent({
  artifact_id: "code-1",
  title: "Snippet",
  artifact: { kind: "code", language: "python", source: "print('hi')" },
});

const imageEvent = artifactEvent({
  artifact_id: "image-1",
  title: "Screenshot",
  artifact: { kind: "image", mime: "image/png", data_base64: "aGk=", width: 4, height: 4 },
});

const events = [jsonEvent, codeEvent, imageEvent];

function sessionPayload(sessionEvents: SessionEvent[], path: string) {
  return {
    version: 2,
    format: "claude",
    path,
    tokens: null,
    base: 0,
    cursor: sessionEvents.length,
    tail_from: 0,
    events: sessionEvents,
    patches: [],
  };
}

beforeEach(() => {
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response("{}", {
    status: 200,
    headers: { "Content-Type": "application/json" },
  }));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ArtifactInspector chrome", () => {
  test("renders dialog with title, kind badge, counter, and actions", () => {
    render(
      <ArtifactInspector
        events={events}
        index={0}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-label")).toBe("Run summary");
    expect(screen.getByText("Run summary")).toBeTruthy();
    expect(screen.getByText("JSON")).toBeTruthy();
    expect(dialog.textContent).toMatch(/1\s*\/\s*3/);
    expect(screen.getByTitle("Copy raw payload")).toBeTruthy();
    expect(screen.getByTitle("Download")).toBeTruthy();
    expect(screen.getByTitle("Close (Esc)")).toBeTruthy();
  });

  test("code artifact carrying a unified diff gets Diff chrome and the diff viewer", () => {
    const diffInCode = artifactEvent({
      artifact_id: "diff-1",
      artifact: {
        kind: "code",
        language: "diff",
        source: "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n",
      },
    });
    render(
      <ArtifactInspector
        events={[diffInCode]}
        index={0}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    const dialog = screen.getByRole("dialog");
    expect(dialog.querySelector(".artifact-inspector-kind")?.textContent).toBe("Diff");
    expect(dialog.getAttribute("aria-label")).toBe("Diff");
    expect(dialog.querySelector(".artifact-detail-diff")).toBeTruthy();
  });

  test("copy image action appears only for image artifacts", () => {
    const { rerender } = render(
      <ArtifactInspector
        events={events}
        index={0}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    expect(screen.queryByTitle("Copy image")).toBeNull();
    rerender(
      <ArtifactInspector
        events={events}
        index={2}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    expect(screen.getByTitle("Copy image")).toBeTruthy();
  });

  test("copy source writes the raw payload to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ArtifactInspector
        events={events}
        index={1}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByTitle("Copy raw payload"));
    });
    expect(writeText).toHaveBeenCalledWith("print('hi')");
  });

  test("copy image reports failure instead of silently copying text", async () => {
    (globalThis as { ClipboardItem?: unknown }).ClipboardItem = class {
      constructor(_types: Record<string, Blob>) {}
    };
    const write = vi.fn().mockResolvedValue(undefined);
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { write, writeText },
    });
    // jsdom has no createImageBitmap/canvas encode, so the PNG conversion
    // throws — the button must surface the failure, not fall back to text.
    render(
      <ArtifactInspector
        events={[imageEvent]}
        index={0}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    await act(async () => {
      fireEvent.click(screen.getByTitle("Copy image"));
    });
    expect(screen.getByText("Copy failed")).toBeTruthy();
    expect(writeText).not.toHaveBeenCalled();
  });
});

describe("ArtifactInspector keyboard", () => {
  test("Escape closes", () => {
    const onClose = vi.fn();
    render(
      <ArtifactInspector
        events={events}
        index={0}
        onClose={onClose}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(onClose).toHaveBeenCalled();
  });

  test("arrow keys step through siblings with wrap-around", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactInspector
        events={events}
        index={0}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
        ticket="WIKI-195"
      />,
    );
    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(onIndexChange).toHaveBeenLastCalledWith(1);
    fireEvent.keyDown(dialog, { key: "ArrowLeft" });
    expect(onIndexChange).toHaveBeenLastCalledWith(2);
  });

  test("single artifact hides pagination and arrows are no-ops", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactInspector
        events={[jsonEvent]}
        index={0}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
        ticket="WIKI-195"
      />,
    );
    expect(screen.queryByLabelText("Previous artifact")).toBeNull();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "ArrowRight" });
    expect(onIndexChange).not.toHaveBeenCalled();
  });

  test("arrow keys are ignored while typing in a text field inside the dialog", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactInspector
        events={events}
        index={0}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
        ticket="WIKI-195"
      />,
    );
    const dialog = screen.getByRole("dialog");
    const input = document.createElement("input");
    dialog.appendChild(input);
    fireEvent.keyDown(input, { key: "ArrowRight" });
    expect(onIndexChange).not.toHaveBeenCalled();
  });

  test("header prev/next buttons navigate", () => {
    const onIndexChange = vi.fn();
    render(
      <ArtifactInspector
        events={events}
        index={1}
        onClose={() => undefined}
        onIndexChange={onIndexChange}
        ticket="WIKI-195"
      />,
    );
    fireEvent.click(screen.getByLabelText("Next artifact"));
    expect(onIndexChange).toHaveBeenLastCalledWith(2);
    fireEvent.click(screen.getByLabelText("Previous artifact"));
    expect(onIndexChange).toHaveBeenLastCalledWith(0);
  });
});

describe("ArtifactInspector focus behavior", () => {
  test("inerts sibling nodes while open and restores on close", () => {
    const outside = document.createElement("div");
    outside.textContent = "background page";
    document.body.appendChild(outside);
    try {
      const { unmount } = render(
        <ArtifactInspector
          events={[jsonEvent]}
          index={0}
          onClose={() => undefined}
          onIndexChange={() => undefined}
          ticket="WIKI-195"
        />,
      );
      expect(outside.hasAttribute("inert")).toBe(true);
      expect(outside.getAttribute("aria-hidden")).toBe("true");
      unmount();
      expect(outside.hasAttribute("inert")).toBe(false);
      expect(outside.getAttribute("aria-hidden")).toBeNull();
    } finally {
      outside.remove();
    }
  });

  test("restores focus to the previously focused element on close", () => {
    const button = document.createElement("button");
    button.textContent = "opener";
    document.body.appendChild(button);
    try {
      button.focus();
      const { unmount } = render(
        <ArtifactInspector
          events={[jsonEvent]}
          index={0}
          onClose={() => undefined}
          onIndexChange={() => undefined}
          ticket="WIKI-195"
        />,
      );
      expect(document.activeElement).toBe(screen.getByRole("dialog"));
      unmount();
      expect(document.activeElement).toBe(button);
    } finally {
      button.remove();
    }
  });

  test("Tab keeps focus inside the dialog", () => {
    render(
      <ArtifactInspector
        events={[jsonEvent]}
        index={0}
        onClose={() => undefined}
        onIndexChange={() => undefined}
        ticket="WIKI-195"
      />,
    );
    const dialog = screen.getByRole("dialog");
    screen.getByTitle("Close (Esc)").focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});

function InspectorScopeHarness({
  primary = false,
  scopeEvents,
  tag,
}: {
  primary?: boolean;
  scopeEvents: SessionEvent[];
  tag: string;
}) {
  const scopeRef = useRef<HTMLDivElement | null>(null);
  const { handleArtifactsChange, inspector, openInspector } = useArtifactInspector({
    primary,
    scopeRef,
    ticket: "WIKI-195",
  });
  useEffect(() => {
    handleArtifactsChange(scopeEvents);
  }, [handleArtifactsChange, scopeEvents]);
  return (
    <div data-scope={tag} ref={scopeRef}>
      {scopeEvents.map((event) => (
        <section data-artifact-id={event.artifact_id} key={event.artifact_id} tabIndex={0}>
          <button onClick={() => openInspector(event)} type="button">open {event.artifact_id}</button>
        </section>
      ))}
      {inspector}
    </div>
  );
}

describe("useArtifactInspector scopes", () => {
  const mainEvents = [jsonEvent, codeEvent];
  const sideEvents = [
    artifactEvent({
      artifact_id: "side-1",
      title: "Side artifact",
      artifact: { kind: "json", json_data: { side: true } },
    }),
  ];

  test("cmd+enter opens the focused artifact in its owning scope with scoped siblings", () => {
    const { container } = render(
      <>
        <InspectorScopeHarness primary scopeEvents={mainEvents} tag="main" />
        <InspectorScopeHarness scopeEvents={sideEvents} tag="side" />
      </>,
    );
    const sideBlock = container.querySelector("[data-artifact-id='side-1']") as HTMLElement;
    sideBlock.focus();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-label")).toBe("Side artifact");
    // one sibling only — no counter, no pagination into the other transcript
    expect(screen.queryByLabelText("Next artifact")).toBeNull();
  });

  test("cmd+enter with no focused/hovered artifact falls back to the primary scope's latest", () => {
    render(
      <>
        <InspectorScopeHarness primary scopeEvents={mainEvents} tag="main" />
        <InspectorScopeHarness scopeEvents={sideEvents} tag="side" />
      </>,
    );
    (document.activeElement as HTMLElement | null)?.blur?.();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    expect(screen.getByRole("dialog").getAttribute("aria-label")).toBe("Snippet");
  });

  test("cmd+enter is inert while an inspector is already open", () => {
    render(<InspectorScopeHarness primary scopeEvents={mainEvents} tag="main" />);
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });

  test("cmd+enter never fires from a text-entry target", () => {
    render(
      <>
        <InspectorScopeHarness primary scopeEvents={mainEvents} tag="main" />
        <textarea aria-label="composer" />
      </>,
    );
    const composer = screen.getByLabelText("composer");
    composer.focus();
    fireEvent.keyDown(composer, { key: "Enter", metaKey: true });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("transcript surfaces", () => {
  test("agents-page drawer artifacts open the fullscreen inspector", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/api/agents/WIKI-DRAWER/session")) {
        return new Response(
          JSON.stringify(sessionPayload([
            artifactEvent({
              artifact_id: "drawer-1",
              title: "Drawer artifact",
              artifact: { kind: "json", json_data: { drawer: true } },
            }),
          ], "drawer.jsonl")),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    render(
      <SessionSidebar
        worker={{ ticket: "WIKI-DRAWER", kind: "cc", role: "implement" }}
        onClose={() => undefined}
        onOpenAgent={() => undefined}
      />,
    );
    const fullscreen = await screen.findByTitle("Fullscreen (⌘↩)");
    fireEvent.click(fullscreen);
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-label")).toBe("Drawer artifact");
  });

  test("subagent transcript artifacts open the fullscreen inspector via cmd+enter", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url.includes("/subagents/sub-1/session")) {
        return new Response(
          JSON.stringify(sessionPayload([
            artifactEvent({
              artifact_id: "sub-artifact-1",
              title: "Subagent artifact",
              artifact: { kind: "json", json_data: { sub: true } },
            }),
          ], "subagent.jsonl")),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    });
    const { container } = render(
      <InspectableSessionTab showComposer={false} subagent="sub-1" ticket="WIKI-SUB" />,
    );
    await screen.findByTitle("Fullscreen (⌘↩)");
    const block = container.querySelector("[data-artifact-id='sub-artifact-1']") as HTMLElement;
    block.focus();
    fireEvent.keyDown(window, { key: "Enter", metaKey: true });
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-label")).toBe("Subagent artifact");
  });
});

describe("helpers", () => {
  test("inspectorTitle prefers event title, then filename, then classified kind label", () => {
    expect(inspectorTitle(jsonEvent)).toBe("Run summary");
    expect(
      inspectorTitle(artifactEvent({
        artifact_id: "code-2",
        artifact: { kind: "code", filename: "main.py", source: "" },
      })),
    ).toBe("main.py");
    expect(
      inspectorTitle(artifactEvent({
        artifact_id: "mermaid-1",
        artifact: { kind: "mermaid", source: "graph TD" },
      })),
    ).toBe("Diagram");
    expect(
      inspectorTitle(artifactEvent({
        artifact_id: "diff-2",
        artifact: { kind: "code", source: "diff --git a/x b/x\n--- a/x\n+++ b/x\n" },
      })),
    ).toBe("Diff");
  });

  test("isTextEntryTarget detects inputs, textareas, and contenteditable", () => {
    expect(isTextEntryTarget(document.createElement("input"))).toBe(true);
    expect(isTextEntryTarget(document.createElement("textarea"))).toBe(true);
    expect(isTextEntryTarget(document.createElement("div"))).toBe(false);
    expect(isTextEntryTarget(null)).toBe(false);
  });
});

describe("ArtifactBlock fullscreen affordance", () => {
  test("fullscreen button calls onInspect with the event", () => {
    const onInspect = vi.fn();
    render(
      <ArtifactBlock
        event={jsonEvent}
        onInspect={onInspect}
        ticket="WIKI-195"
      />,
    );
    fireEvent.click(screen.getByTitle("Fullscreen (⌘↩)"));
    expect(onInspect).toHaveBeenCalledWith(jsonEvent);
  });

  test("block is focusable only when inspection is wired", () => {
    const { container, rerender } = render(
      <ArtifactBlock event={jsonEvent} onInspect={() => undefined} ticket="WIKI-195" />,
    );
    const section = container.querySelector("[data-artifact-id='json-1']") as HTMLElement;
    expect(section.getAttribute("tabindex")).toBe("0");
    rerender(<ArtifactBlock event={jsonEvent} ticket="WIKI-195" />);
    expect(section.getAttribute("tabindex")).toBeNull();
    expect(screen.queryByTitle("Fullscreen (⌘↩)")).toBeNull();
  });
});
