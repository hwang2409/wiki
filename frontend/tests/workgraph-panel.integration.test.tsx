// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { AgentWorkgraphData, WorkgraphRevision } from "../src/api";
import { groupEdges, WorkgraphPanel } from "../src/workgraph-panel";

const revisions: WorkgraphRevision[] = [
  { revision: 5, created_at_ns: 1_784_700_005_000_000_000, edge_count: 1 },
  { revision: 10, created_at_ns: 1_784_700_010_000_000_000, edge_count: 2 },
  { revision: 15, created_at_ns: 1_784_700_015_000_000_000, edge_count: 3 },
];

function graphData(revision: number): AgentWorkgraphData {
  return {
    ok: true,
    source: "snapshot",
    revision,
    workgraph: {
      ticket: "WIKI-169",
      orch: "wiki",
      created_at: "2026-07-29T12:00:00Z",
      updated_at: "2026-07-29T12:00:00Z",
      nodes: [
        { id: "N-1", kind: "orchestrator", label: "wiki orch" },
        { id: "N-2", kind: "worker", label: `revision-${revision}` },
      ],
      edges: Array.from({ length: Math.max(1, revision / 5) }, () => ({
        kind: "spawn",
        from: "N-1",
        to: "N-2",
        created_at: "2026-07-29T12:00:00Z",
        payload: {},
      })),
      composite_health: {
        state: "iterating",
        open_findings: 0,
        blocking: 0,
        slowest_node_stall_seconds: 0,
        iteration_count: 0,
      },
    },
  };
}

const pending: Array<{
  revision: number;
  resolve: (data: AgentWorkgraphData) => void;
}> = [];
const aborted: number[] = [];

vi.mock("../src/api", () => ({
  getAgentWorkgraph: vi.fn(async () => graphData(15)),
  getAgentWorkgraphRevisions: vi.fn(async () => revisions),
  getAgentWorkgraphRevision: vi.fn(
    async (_ticket: string, revision: number, signal?: AbortSignal) =>
      new Promise<AgentWorkgraphData>((resolve) => {
        pending.push({ revision, resolve });
        signal?.addEventListener("abort", () => aborted.push(revision), { once: true });
      })
  ),
}));

describe("workgraph revision loading", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    pending.length = 0;
    aborted.length = 0;
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  test("aborts superseded revision requests and never renders stale graphs", async () => {
    render(<WorkgraphPanel ticket="WIKI-169" tick={0} />);
    await act(async () => {
      await Promise.resolve();
    });

    fireEvent.click(screen.getByRole("tab", { name: "replay" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(150);
    });
    expect(pending.map((request) => request.revision)).toEqual([15]);
    await act(async () => {
      pending[0].resolve(graphData(15));
      await Promise.resolve();
    });

    const slider = screen.getByRole("slider", { name: "Timeline revision" });
    await act(async () => {
      fireEvent.change(slider, { target: { value: "0" } });
      await vi.advanceTimersByTimeAsync(150);
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.change(slider, { target: { value: "1" } });
      await vi.advanceTimersByTimeAsync(150);
      await Promise.resolve();
    });
    await act(async () => {
      fireEvent.change(slider, { target: { value: "2" } });
      await vi.advanceTimersByTimeAsync(150);
      await Promise.resolve();
    });

    expect(aborted).toEqual([15, 5, 10]);
    expect(pending.map((request) => request.revision)).toEqual([15, 5, 10, 15]);

    await act(async () => {
      pending[1].resolve(graphData(5));
      pending[2].resolve(graphData(10));
      pending[3].resolve(graphData(15));
      await Promise.resolve();
    });
    expect(screen.getByText("revision-15")).toBeTruthy();
    expect(screen.queryByText("revision-5")).toBeNull();
    expect(screen.queryByText("revision-10")).toBeNull();
  });
});

test("legacy workgraph edges without active keep the historical style", () => {
  const [edge] = groupEdges([
    {
      kind: "spawn",
      from: "orch:wiki",
      to: "WIKI-170",
      created_at: "2026-07-29T12:00:00Z",
    },
  ]);

  expect(edge.active).toBe(false);
});
