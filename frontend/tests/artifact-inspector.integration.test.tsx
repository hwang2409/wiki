// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { SessionEvent } from "../src/api";
import { ArtifactBlock } from "../src/artifact-block";
import {
  ArtifactInspector,
  inspectorTitle,
  isTextEntryTarget,
  resolveInspectTarget,
} from "../src/artifact-inspector";

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

describe("resolveInspectTarget", () => {
  test("focused artifact wins over everything", () => {
    const host = document.createElement("section");
    host.setAttribute("data-artifact-id", "code-1");
    host.tabIndex = 0;
    document.body.appendChild(host);
    try {
      host.focus();
      expect(resolveInspectTarget(document.body, events, "image-1")).toBe(1);
    } finally {
      host.remove();
    }
  });

  test("falls back to the panel's focused tab", () => {
    expect(resolveInspectTarget(document.body, events, "image-1")).toBe(2);
  });

  test("falls back to the most recent artifact", () => {
    expect(resolveInspectTarget(document.body, events, null)).toBe(2);
  });

  test("returns null when there are no artifacts", () => {
    expect(resolveInspectTarget(document.body, [], null)).toBeNull();
  });
});

describe("helpers", () => {
  test("inspectorTitle prefers event title, then filename, then kind label", () => {
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
