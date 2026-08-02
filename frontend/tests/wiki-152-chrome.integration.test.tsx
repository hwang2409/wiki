// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { ProviderActionRequired } from "../src/session";
import { WorkerStatePill } from "../src/agent-session-surface";
import type { ProviderEventInspector, ProviderPendingRequest } from "../src/api";

function inspector(overrides: Partial<ProviderEventInspector> = {}): ProviderEventInspector {
  return {
    run_id: "run-1",
    provider: "codex",
    state: "working",
    raw_count: 12,
    normalized_count: 12,
    dispositions: { rendered: 12, summarized: 0, ignored: 0, unknown: 0 },
    pending_requests: [],
    events: [],
    ...overrides,
  };
}

function pending(overrides: Partial<ProviderPendingRequest> = {}): ProviderPendingRequest {
  return {
    request_id: 42,
    request_kind: "item/tool/requestUserInput",
    received_at: "2026-07-30T00:00:00Z",
    raw_seq: 1,
    payload: { method: "item/tool/requestUserInput", params: {} },
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("WIKI-152 action-required panel", () => {
  test("renders pending requests outside the diagnostics disclosure", () => {
    render(<ProviderActionRequired inspector={inspector({ pending_requests: [pending()] })} ticket="WIKI-152" />);
    const panel = screen.getByTestId("session-action-required");
    expect(panel).toBeTruthy();
    // Heading matches spec — one canonical "Action required" label per panel, not per card.
    const heading = within(panel).getByText("Action required");
    expect(heading).toBeTruthy();
    expect(within(panel).queryAllByText("Action required")).toHaveLength(1);
  });

  test("returns null when no pending requests", () => {
    const { container } = render(
      <ProviderActionRequired inspector={inspector({ pending_requests: [] })} ticket="WIKI-152" />,
    );
    expect(container.querySelector(".session-action-required")).toBeNull();
  });

  test("shows count badge when there is more than one pending request", () => {
    render(
      <ProviderActionRequired
        inspector={inspector({
          pending_requests: [pending({ request_id: 1 }), pending({ request_id: 2 })],
        })}
        ticket="WIKI-152"
      />,
    );
    const panel = screen.getByTestId("session-action-required");
    const badge = panel.querySelector(".session-action-required-count");
    expect(badge?.textContent).toBe("2");
  });
});

describe("WIKI-152 worker state pill", () => {
  test("returns null when state is missing", () => {
    const { container } = render(<WorkerStatePill state={null} />);
    expect(container.querySelector(".session-state-pill")).toBeNull();
  });

  test.each([
    ["merge-ready", "is-positive"],
    ["working", "is-neutral"],
    ["blocked", "is-negative"],
    ["completed", "is-faint"],
  ])("maps %s to %s", (state, tone) => {
    render(<WorkerStatePill state={state} />);
    const pill = screen.getByTestId("session-state-pill");
    expect(pill.classList.contains(tone)).toBe(true);
    expect(pill.textContent).toBe(state);
  });
});
