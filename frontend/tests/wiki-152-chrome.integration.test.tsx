// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";

import { ProviderActionRequired, SessionRunDetails } from "../src/session";
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

describe("WIKI-152 default chrome — no diagnostic noise", () => {
  test("SessionRunDetails is collapsed by default and hides Provider stream label", () => {
    render(
      <SessionRunDetails
        inspector={inspector()}
        format="msg/v1"
        tokens={3200}
        thinkingTokens={800}
        dispositions={null}
      />,
    );
    const details = screen.getByTestId("session-run-details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    // Summary shows compact label — not the ambient "Provider stream" chip.
    expect(screen.getByText("Run details")).toBeTruthy();
    expect(screen.queryByText("Provider stream")).toBeNull();
    // Summary carries a compact provider · state · tokens hint that is not a raw count.
    expect(screen.getByText(/codex · working · 3k tok/)).toBeTruthy();
    // The summary itself does not leak the raw disposition string; that is body-only.
    const summary = details.querySelector("summary");
    expect(summary?.textContent ?? "").not.toContain("Unknown");
  });

  test("opening SessionRunDetails reveals raw→normalized and disposition counts", () => {
    render(<SessionRunDetails inspector={inspector({ raw_count: 4, normalized_count: 4 })} format={null} tokens={null} thinkingTokens={null} dispositions={null} />);
    const summary = screen.getByText("Run details");
    fireEvent.click(summary);
    const details = screen.getByTestId("session-run-details") as HTMLDetailsElement;
    expect(details.open).toBe(true);
    expect(within(details).getByText("raw 4 → normalized 4")).toBeTruthy();
    const meta = details.querySelector(".session-run-details-meta");
    expect(meta?.textContent ?? "").toContain("provider");
    expect(meta?.textContent ?? "").toContain("codex");
  });

  test("SessionRunDetails returns null when there is nothing to show", () => {
    const { container } = render(
      <SessionRunDetails inspector={null} format={null} tokens={null} thinkingTokens={null} dispositions={null} />,
    );
    expect(container.querySelector(".session-run-details")).toBeNull();
  });
});

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
