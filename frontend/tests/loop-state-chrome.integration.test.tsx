// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { AgentWorkgraphData, LoopState } from "../src/api";
import { LoopStateChrome } from "../src/loop-state-chrome";

const loopState: LoopState = {
  round: 2,
  cap: 8,
  danger: "normal",
  unrouted_verdict_count: 1,
  plateau_length: 2,
  latest_verdict: null,
  latest_verdict_finding: "example finding title",
  history: [
    {
      round: 1,
      reviewer: "WIKI-000-REVIEW1",
      spawned_at: "2026-07-29T08:00:00Z",
      verdict_state: "NOT-MERGE-READY",
      verdict_at: "2026-07-29T08:10:00Z",
      routed_at: "2026-07-29T08:12:00Z",
      archived_at: "2026-07-29T08:15:00Z",
      top_finding: "example finding title",
      finding_signature: "example finding title",
    },
    {
      round: 2,
      reviewer: "WIKI-000-REVIEW2",
      spawned_at: "2026-07-29T09:00:00Z",
      verdict_state: "NOT-MERGE-READY",
      verdict_at: "2026-07-29T09:10:00Z",
      routed_at: null,
      archived_at: null,
      top_finding: "example finding title",
      finding_signature: "example finding title",
    },
  ],
};

const payload: AgentWorkgraphData = {
  ok: true,
  source: "live",
  workgraph: {
    ticket: "WIKI-000",
    orch: "wiki",
    created_at: "2026-07-29T08:00:00Z",
    updated_at: "2026-07-29T09:10:00Z",
    nodes: [
      { id: "wiki:main", kind: "orchestrator", label: "wiki orch" },
      { id: "WIKI-000", kind: "implement", label: "WIKI-000" },
    ],
    edges: [],
    composite_health: {
      state: "iterating",
      open_findings: 0,
      blocking: 0,
      slowest_node_stall_seconds: 0,
      iteration_count: 0,
    },
  },
  loop_state: loopState,
};

describe("LoopStateChrome", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }))
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    cleanup();
  });

  test("pointer sequence on trigger toggles cleanly (mousedown + click do not fight)", async () => {
    render(<LoopStateChrome ticket="WIKI-000" tick={0} />);

    // Wait for the trigger to appear once the mocked fetch resolves.
    const trigger = await screen.findByRole("button", {
      name: /show merge-ready loop history/i,
    });

    expect(trigger.getAttribute("aria-expanded")).toBe("false");

    // Simulate a real pointer press: mousedown first (which is what the
    // outside-click handler listens for), then the follow-up click that
    // React's onClick runs. Before the containment fix the mousedown
    // closed the (already-closed) panel and the click reopened it — net
    // state was inverted vs. an idle toggle. Now the sequence must land
    // on aria-expanded="true".
    act(() => {
      fireEvent.mouseDown(trigger);
      fireEvent.click(trigger);
    });

    await waitFor(() => {
      expect(trigger.getAttribute("aria-expanded")).toBe("true");
    });

    // Second pointer sequence must close cleanly, not close-then-reopen.
    act(() => {
      fireEvent.mouseDown(trigger);
      fireEvent.click(trigger);
    });

    await waitFor(() => {
      expect(trigger.getAttribute("aria-expanded")).toBe("false");
    });
  });

  test("mousedown outside trigger and detail still closes the panel", async () => {
    render(
      <div>
        <div data-testid="outside" style={{ width: 40, height: 40 }} />
        <LoopStateChrome ticket="WIKI-000" tick={0} />
      </div>
    );
    const trigger = await screen.findByRole("button", {
      name: /show merge-ready loop history/i,
    });
    act(() => {
      fireEvent.mouseDown(trigger);
      fireEvent.click(trigger);
    });
    await waitFor(() => {
      expect(trigger.getAttribute("aria-expanded")).toBe("true");
    });
    const outside = screen.getByTestId("outside");
    act(() => {
      fireEvent.mouseDown(outside);
    });
    await waitFor(() => {
      expect(trigger.getAttribute("aria-expanded")).toBe("false");
    });
  });
});
