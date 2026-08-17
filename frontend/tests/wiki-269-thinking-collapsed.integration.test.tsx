// @vitest-environment jsdom
// WIKI-269: thinking summaries collapse by default so a transcript full of
// reasoning rows stays scannable. Click toggles per-row; toggle state is
// component-local (session-scoped, not persisted across app restarts).
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import type { SessionEvent } from "../src/api";
import { ThinkingRow } from "../src/session";

afterEach(() => {
  cleanup();
});

function thinkingEvent(overrides: Partial<SessionEvent> = {}): SessionEvent {
  return {
    id: 1,
    kind: "thinking",
    ts: "2026-08-07T12:00:00.000Z",
    text: "**Weighing the approach**\nConsidering two paths through the parser.",
    disposition: "rendered",
    ...overrides,
  };
}

describe("thinking rows collapse by default", () => {
  test("completed row renders collapsed with only the preview line", () => {
    const { container } = render(<ThinkingRow event={thinkingEvent()} />);
    const head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    expect(head).not.toBeNull();
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".session-thinking-prefix")?.textContent).toBe("+");
    expect(container.querySelector(".session-thinking")).toBeNull();
    expect(container.querySelector(".session-thinking-title")?.textContent).toBe("**Weighing the approach**");
  });

  test("codex encrypted summary also renders collapsed (regression)", () => {
    // Before WIKI-269 the encrypted marker auto-opened the body — the visible
    // symptom Henry flagged. A completed encrypted thought must render closed.
    const { container } = render(<ThinkingRow event={thinkingEvent({ encrypted: true })} />);
    const head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".session-thinking")).toBeNull();
  });

  test("codex preview strips bold markers and separates adjacent summary sections", () => {
    const { container } = render(
      <ThinkingRow
        event={thinkingEvent({
          encrypted: true,
          text: "**diagnosing duplicate sed output and planning harness simplification****planning incremental dashboard…",
        })}
      />,
    );
    const head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    expect(container.querySelector(".session-thinking-title")?.textContent).toBe(
      "diagnosing duplicate sed output and planning harness simplification · planning incremental dashboard…",
    );
    expect(head.textContent).not.toContain("**");

    fireEvent.click(head);
    expect(container.querySelectorAll(".session-thinking strong")).toHaveLength(2);
    expect(container.querySelector(".session-thinking")?.textContent).toContain(
      "diagnosing duplicate sed output and planning harness simplification · planning incremental dashboard…",
    );
  });

  test("clean Codex thought text passes through unchanged", () => {
    const { container } = render(
      <ThinkingRow event={thinkingEvent({ encrypted: true, text: "planning the next step" })} />,
    );
    expect(container.querySelector(".session-thinking-title")?.textContent).toBe(
      "planning the next step",
    );
  });

  test("click expands the body, second click collapses it", () => {
    const { container } = render(<ThinkingRow event={thinkingEvent()} />);
    const head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    fireEvent.click(head);
    expect(head.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector(".session-thinking-prefix")?.textContent).toBe("-");
    expect(container.querySelector(".session-thinking")).not.toBeNull();

    fireEvent.click(head);
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".session-thinking-prefix")?.textContent).toBe("+");
    expect(container.querySelector(".session-thinking")).toBeNull();
  });

  test("per-row toggle state is independent between siblings", () => {
    const { container } = render(
      <>
        <ThinkingRow event={thinkingEvent({ id: 1, text: "**First**\nfirst body" })} />
        <ThinkingRow event={thinkingEvent({ id: 2, text: "**Second**\nsecond body" })} />
      </>,
    );
    const heads = container.querySelectorAll(".session-thinking-head");
    expect(heads).toHaveLength(2);
    fireEvent.click(heads[0]);
    expect(heads[0].getAttribute("aria-expanded")).toBe("true");
    expect(heads[1].getAttribute("aria-expanded")).toBe("false");
  });

  test("streaming updates to the same event keep the row collapsed on completion", () => {
    // A thinking event's text grows in-place as deltas arrive; when the row
    // is done streaming, it must not pop open. The default is collapsed and
    // stays collapsed regardless of text growth — user click is the only
    // path to open.
    const { container, rerender } = render(
      <ThinkingRow event={thinkingEvent({ text: "**Deciding**\npartial" })} />,
    );
    let head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    expect(head.getAttribute("aria-expanded")).toBe("false");

    rerender(
      <ThinkingRow event={thinkingEvent({ text: "**Deciding**\npartial\ncompleted body" })} />,
    );
    head = container.querySelector(".session-thinking-head") as HTMLButtonElement;
    expect(head.getAttribute("aria-expanded")).toBe("false");
    expect(container.querySelector(".session-thinking")).toBeNull();
  });
});
