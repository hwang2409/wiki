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
  test("SessionRunDetails collapsed summary shows label only and leaks no telemetry", () => {
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
    // Summary label reads "Run details" — nothing more.
    expect(screen.getByText("Run details")).toBeTruthy();
    // R1-01: the collapsed summary must not surface provider, state, tokens,
    // format, or disposition strings — a leak makes the disclosure the noise
    // it was meant to hide.
    const summary = details.querySelector("summary");
    const summaryText = summary?.textContent ?? "";
    for (const leak of ["Provider stream", "codex", "working", "tok", "msg/v1", "Unknown"]) {
      expect(summaryText).not.toContain(leak);
    }
    // Screen-level: none of the demoted labels render outside the collapsed body.
    expect(screen.queryByText("Provider stream")).toBeNull();
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

  test("R2-01: prefers inspector dispositions over session-level fallback", () => {
    // Divergent counts: inspector saw real events, session-level fallback
    // is zeroed (the headless-fallback case that used to resurrect
    // "Unknown 0" in real live provider streams).
    render(
      <SessionRunDetails
        inspector={inspector({
          dispositions: { rendered: 5, summarized: 1, ignored: 0, unknown: 3 },
        })}
        format={null}
        tokens={null}
        thinkingTokens={null}
        dispositions={{ rendered: 0, summarized: 0, ignored: 0, unknown: 0 }}
      />,
    );
    const details = screen.getByTestId("session-run-details") as HTMLDetailsElement;
    details.setAttribute("open", "");
    const meta = details.querySelector(".session-run-details-meta");
    expect(meta?.textContent ?? "").toContain("Unknown 3");
    expect(meta?.textContent ?? "").not.toContain("Unknown 0");
  });

  test("R2-02: pending requests render inside Run details even with an empty event log", () => {
    render(
      <SessionRunDetails
        inspector={inspector({
          events: [],
          pending_requests: [
            {
              request_id: 0,
              request_kind: "item/tool/requestUserInput",
              received_at: "2026-07-30T00:00:00Z",
              raw_seq: 1,
              payload: { method: "item/tool/requestUserInput", params: {} },
            },
          ],
        })}
        format={null}
        tokens={null}
        thinkingTokens={null}
        dispositions={null}
      />,
    );
    const details = screen.getByTestId("session-run-details") as HTMLDetailsElement;
    details.setAttribute("open", "");
    const pending = screen.getByTestId("run-details-pending-requests");
    expect(pending.textContent).toContain("pending");
    // request_id 0 must be visible — the falsy check that used to swallow
    // it in the action-required card cannot leave it invisible everywhere.
    expect(pending.textContent).toContain("id #0");
    expect(pending.textContent).toContain("item/tool/requestUserInput");
    // The "no normalized provider events yet" empty-state should be
    // suppressed when a pending request is showing (otherwise it reads
    // as broken).
    expect(details.querySelector(".session-provider-empty")).toBeNull();
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
