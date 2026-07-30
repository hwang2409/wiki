// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ActivityFeed } from "../src/activity";
import { GraphView } from "../src/graph";
import { HealthView } from "../src/health";
import { TokensView } from "../src/tokens";
import type { NoteSummary } from "../src/types";

const NOTES_FIXTURE: NoteSummary[] = [
  {
    id: "note-1",
    path: "meta/hot.md",
    title: "Hot",
    note_type: "reference",
    updated_at: new Date(Date.now() - 90 * 24 * 60 * 60 * 1000).toISOString(),
    meta_updated: new Date(Date.now() - 90 * 24 * 60 * 60 * 1000).toISOString(),
    tags: [],
    aliases: [],
  } as unknown as NoteSummary,
];

function jsonResponse<T>(body: T, ok = true, status = 200): Response {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function installFetch(handler: (input: RequestInfo | URL) => Promise<Response>) {
  const original = globalThis.fetch;
  globalThis.fetch = vi.fn(handler as typeof fetch);
  return () => {
    globalThis.fetch = original;
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("activity page: loading -> error -> retry recovers to data", async () => {
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) throw new Error("network down");
    return jsonResponse([
      {
        sha: "abcdef1234567890",
        date: "2026-07-30T10:15:00+00:00",
        message: "seed note",
        files: [{ path: "vault/notes/seed.md", status: "A" }],
      },
    ]);
  });
  try {
    render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    expect(screen.getByText(/Activity feed/i)).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Activity is unavailable");
    });

    fireEvent.click(screen.getByRole("button", { name: /Retry/i }));

    await waitFor(() => {
      expect(screen.getByText("seed note")).toBeTruthy();
    });
    // Short sha is shown as secondary information (git terminology demoted).
    expect(screen.getByText("abcdef1")).toBeTruthy();
  } finally {
    restore();
  }
});

test("activity page: empty state renders when there are no commits", async () => {
  const restore = installFetch(async () => jsonResponse([]));
  try {
    render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    await waitFor(() => {
      expect(screen.getByText(/No vault activity yet/i)).toBeTruthy();
    });
  } finally {
    restore();
  }
});

test("health page: chrome + empty state, no primary CLI reference", () => {
  render(<HealthView notes={[]} onOpenNote={() => {}} />);
  expect(screen.getByText("Vault health")).toBeTruthy();
  expect(screen.getByText(/No notes in the vault yet/i)).toBeTruthy();
  // Regression guard: WIKI-157 demoted the primary "wiki lint" call-to-action.
  expect(screen.queryByText(/wiki lint/)).toBeNull();
});

test("health page: renders living notes when provided", () => {
  render(<HealthView notes={NOTES_FIXTURE} onOpenNote={() => {}} />);
  expect(screen.getByText("hot")).toBeTruthy();
});

test("graph page: loading -> data -> canvas/list toggle exists", async () => {
  const restore = installFetch(async () =>
    jsonResponse({
      "notes/a.md": { outgoing: ["notes/b.md"], incoming: [], unresolved: [] },
      "notes/b.md": { outgoing: [], incoming: ["notes/a.md"], unresolved: ["ghost.md"] },
    }),
  );
  try {
    render(<GraphView onOpenNote={() => {}} />);
    expect(screen.getByText("Graph view")).toBeTruthy();

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: /^List$/i, pressed: false }),
      ).toBeTruthy();
    });

    fireEvent.click(screen.getByRole("button", { name: /^List$/i }));

    await waitFor(() => {
      expect(screen.getByText("a")).toBeTruthy();
      expect(screen.getByText("b")).toBeTruthy();
      expect(screen.getByText("ghost.md")).toBeTruthy();
    });
    // Unresolved rows carry the disabled affordance.
    const ghostRow = screen.getByText("ghost.md").closest("button");
    expect(ghostRow?.hasAttribute("disabled")).toBe(true);
  } finally {
    restore();
  }
});

test("graph page: error state exposes a retry button", async () => {
  const restore = installFetch(async () => {
    throw new Error("api down");
  });
  try {
    render(<GraphView onOpenNote={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Link graph is unavailable");
    });
    expect(screen.getByRole("button", { name: /Retry/i })).toBeTruthy();
  } finally {
    restore();
  }
});

test("tokens page: labels reasoning/cached as unavailable when the API omits them", async () => {
  const restore = installFetch(async () =>
    jsonResponse({
      buckets: [
        {
          ts: "2026-07-30T10:00:00+00:00",
          // Reasoning + cached deliberately omitted — WIKI-157 must render
          // these as "unavailable" instead of the misleading 0.
          series: { "claude/opus-4-7": { input: 1200, output: 800 } },
        },
      ],
      totals: { input: 1200, cached: 0, output: 800, reasoning: 0 },
      models: ["opus-4-7"],
      clis: ["claude"],
      sessions_scanned: 3,
      bucket: "hour",
      refreshing: false,
    }),
  );
  try {
    render(<TokensView />);
    expect(screen.getByText("Token usage")).toBeTruthy();

    await waitFor(() => {
      expect(screen.getByText("1,200")).toBeTruthy();
    });

    const unavailable = screen.getAllByText("unavailable");
    // reasoning + cached should both be flagged unavailable.
    expect(unavailable.length).toBeGreaterThanOrEqual(2);

    // The filter label demotes "cli" in favor of the sidebar-consistent "agent".
    expect(screen.getByLabelText(/Agent filter/i)).toBeTruthy();
  } finally {
    restore();
  }
});

test("tokens page: empty state on zero buckets, no bare 0 chart", async () => {
  const restore = installFetch(async () =>
    jsonResponse({
      buckets: [],
      totals: { input: 0, cached: 0, output: 0, reasoning: 0 },
      models: [],
      clis: [],
      sessions_scanned: 0,
      bucket: "hour",
      refreshing: false,
    }),
  );
  try {
    render(<TokensView />);
    await waitFor(() => {
      expect(screen.getByText(/No token usage in this range/i)).toBeTruthy();
    });
  } finally {
    restore();
  }
});

test("tokens page: error state exposes a retry", async () => {
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) throw new Error("api unreachable");
    return jsonResponse({
      buckets: [],
      totals: { input: 0, cached: 0, output: 0, reasoning: 0 },
      models: [],
      clis: [],
      sessions_scanned: 0,
      bucket: "hour",
      refreshing: false,
    });
  });
  try {
    render(<TokensView />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain(
        "Token usage is unavailable",
      );
    });
    fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
    await waitFor(() => {
      expect(screen.queryByRole("alert")).toBeNull();
    });
  } finally {
    restore();
  }
});
