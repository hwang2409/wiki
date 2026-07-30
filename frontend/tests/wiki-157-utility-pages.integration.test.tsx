// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ActivityFeed } from "../src/activity";
import { GraphView } from "../src/graph";
import { HealthView, calendarDayDiff, parseNoteUpdated } from "../src/health";
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

test("activity page: refresh failure preserves stale entries with a retry banner", async () => {
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) {
      return jsonResponse([
        {
          sha: "aaa1111",
          date: "2026-07-30T09:00:00+00:00",
          message: "seed message",
          files: [{ path: "vault/notes/one.md", status: "A" }],
        },
      ]);
    }
    throw new Error("refresh boom");
  });
  try {
    const { rerender } = render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    await waitFor(() => {
      expect(screen.getByText("seed message")).toBeTruthy();
    });
    rerender(<ActivityFeed onOpenNote={() => {}} refreshTick={1} />);
    await waitFor(() => {
      // Loaded entry survives the refresh failure.
      expect(screen.getByText("seed message")).toBeTruthy();
      // Non-blocking banner is present with a retry affordance.
      const banner = document.querySelector<HTMLElement>(".activity-refresh-banner");
      expect(banner).toBeTruthy();
      expect(banner!.textContent).toContain("refresh boom");
      expect(
        banner!.querySelector<HTMLButtonElement>(".activity-refresh-retry"),
      ).toBeTruthy();
    });
    // Regression: refresh failure must NOT swap the body out for the error state.
    expect(screen.queryByText(/Activity is unavailable/i)).toBeNull();
  } finally {
    restore();
  }
});

test("activity page: day heading is friendly, status codes are labels", async () => {
  const today = new Date();
  const iso = new Date(
    today.getFullYear(),
    today.getMonth(),
    today.getDate(),
    10,
    15,
  ).toISOString();
  const restore = installFetch(async () =>
    jsonResponse([
      {
        sha: "abcdef1234567890",
        date: iso,
        message: "seed",
        files: [
          { path: "vault/notes/seed.md", status: "A" },
          { path: "vault/notes/plan.md", status: "M" },
          { path: "vault/notes/old.md", status: "D" },
        ],
      },
    ]),
  );
  try {
    render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    await waitFor(() => {
      expect(document.querySelector(".activity-message")).toBeTruthy();
    });
    // Day heading is friendly, not raw YYYY-MM-DD.
    const heading = document.querySelector<HTMLElement>(".activity-day-heading");
    expect(heading?.textContent ?? "").toMatch(/Today/i);
    // Status codes A / M / D render as full labels, not single letters.
    const statuses = Array.from(
      document.querySelectorAll<HTMLElement>(".activity-file-status"),
    ).map((el) => el.textContent);
    expect(statuses).toContain("added");
    expect(statuses).toContain("modified");
    expect(statuses).toContain("deleted");
  } finally {
    restore();
  }
});

test("health page: loading state does NOT read as empty vault", () => {
  render(
    <HealthView
      error={null}
      loading
      notes={[]}
      notesLoaded={false}
      onOpenNote={() => {}}
      onRetry={() => {}}
    />,
  );
  expect(screen.getByText("Note freshness")).toBeTruthy();
  expect(screen.getByRole("status").textContent ?? "").toContain("Reading vault notes");
  // BLOCKING guard: booting must NOT surface the vault-empty state.
  expect(screen.queryByText(/No notes in the vault yet/i)).toBeNull();
  // Round-3 review: rename dropped the "agent memory rots" loaded language.
  expect(document.body.textContent ?? "").not.toMatch(/agent memory rots/i);
});

test("health page: boot failure surfaces retry, calls onRetry", () => {
  const onRetry = vi.fn();
  render(
    <HealthView
      error="notes endpoint 500"
      loading={false}
      notes={[]}
      notesLoaded={false}
      onOpenNote={() => {}}
      onRetry={onRetry}
    />,
  );
  expect(screen.getByRole("alert").textContent).toContain("Note freshness is unavailable");
  fireEvent.click(screen.getByRole("button", { name: /Retry/i }));
  expect(onRetry).toHaveBeenCalledTimes(1);
});

test("health page: empty state renders only when notes really are empty", () => {
  render(
    <HealthView
      error={null}
      loading={false}
      notes={[]}
      notesLoaded
      onOpenNote={() => {}}
      onRetry={() => {}}
    />,
  );
  expect(screen.getByText(/No notes in the vault yet/i)).toBeTruthy();
  expect(screen.queryByText(/wiki lint/)).toBeNull();
});

test("health page: renders living notes when provided", () => {
  render(
    <HealthView
      error={null}
      loading={false}
      notes={NOTES_FIXTURE}
      notesLoaded
      onOpenNote={() => {}}
      onRetry={() => {}}
    />,
  );
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
      const rows = document.querySelectorAll(".graph-list-row .graph-list-name");
      const labels = Array.from(rows).map((row) => row.textContent);
      expect(labels).toContain("a");
      expect(labels).toContain("b");
      expect(labels).toContain("ghost.md");
    });
    // Ghost row is marked unresolved at the item level.
    const unresolvedItems = Array.from(
      document.querySelectorAll(".graph-list-item.is-unresolved"),
    );
    const ghostLabels = unresolvedItems
      .map((item) => item.querySelector(".graph-list-name")?.textContent)
      .filter(Boolean);
    expect(ghostLabels).toContain("ghost.md");
  } finally {
    restore();
  }
});

test("graph page: subtitle counts notes and unresolved targets separately", async () => {
  const restore = installFetch(async () =>
    jsonResponse({
      "notes/a.md": { outgoing: ["notes/b.md"], incoming: [], unresolved: ["future.md"] },
      "notes/b.md": { outgoing: [], incoming: ["notes/a.md"], unresolved: [] },
    }),
  );
  try {
    render(<GraphView onOpenNote={() => {}} />);
    await waitFor(() => {
      const subtitle = document.querySelector<HTMLElement>(".utility-page-subtitle");
      expect(subtitle).toBeTruthy();
      expect(subtitle!.textContent).toContain("2 notes");
      expect(subtitle!.textContent).toContain("1 unresolved target");
      // Regression: unresolved must not roll into the note count.
      expect(subtitle!.textContent).not.toContain("3 notes");
    });
  } finally {
    restore();
  }
});

test("graph page: retry re-fetches and recovers data", async () => {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    if (String(input).includes("/api/links")) {
      if (fetchSpy.mock.calls.length === 1) throw new Error("api down");
      return jsonResponse({
        "notes/root.md": { outgoing: ["notes/branch.md"], incoming: [], unresolved: [] },
        "notes/branch.md": { outgoing: [], incoming: ["notes/root.md"], unresolved: [] },
      });
    }
    return jsonResponse({});
  });
  const original = globalThis.fetch;
  globalThis.fetch = fetchSpy as typeof fetch;
  try {
    render(<GraphView onOpenNote={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Link graph is unavailable");
    });
    expect(fetchSpy).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: /Retry/i }));

    // Second fetch must actually happen, and recovered data must render.
    await waitFor(() => {
      expect(fetchSpy).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("alert")).toBeNull();
    });

    fireEvent.click(screen.getByRole("button", { name: /^List$/i }));
    await waitFor(() => {
      const rows = document.querySelectorAll(".graph-list-row .graph-list-name");
      const labels = Array.from(rows).map((r) => r.textContent);
      expect(labels).toContain("root");
      expect(labels).toContain("branch");
    });
  } finally {
    globalThis.fetch = original;
  }
});

test("graph page: list mode exposes outgoing + incoming edges per node", async () => {
  const restore = installFetch(async () =>
    jsonResponse({
      "notes/root.md": { outgoing: ["notes/leaf.md"], incoming: [], unresolved: ["future.md"] },
      "notes/leaf.md": { outgoing: [], incoming: ["notes/root.md"], unresolved: [] },
    }),
  );
  try {
    render(<GraphView onOpenNote={() => {}} />);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^List$/i })).toBeTruthy();
    });
    fireEvent.click(screen.getByRole("button", { name: /^List$/i }));
    await waitFor(() => {
      expect(document.querySelector(".graph-list-item")).toBeTruthy();
    });

    // Expand the root disclosure and check both edge groups render.
    const rootRow = Array.from(document.querySelectorAll(".graph-list-row")).find(
      (row) => row.querySelector(".graph-list-name")?.textContent === "root",
    ) as HTMLElement | undefined;
    if (!rootRow) throw new Error("root row not found");
    fireEvent.click(rootRow);

    const rootDetails = rootRow.closest("details");
    if (!rootDetails) throw new Error("root details not found");

    await waitFor(() => {
      expect(rootDetails.hasAttribute("open")).toBe(true);
    });
    const outgoingGroup = rootDetails.querySelector<HTMLElement>(
      '[aria-label="Outgoing wikilinks"]',
    );
    const incomingGroup = rootDetails.querySelector<HTMLElement>(
      '[aria-label="Incoming wikilinks"]',
    );
    expect(outgoingGroup).toBeTruthy();
    expect(incomingGroup).toBeTruthy();
    // Outgoing should surface both the resolved leaf and the unresolved ghost.
    expect(outgoingGroup!.textContent).toContain("leaf");
    expect(outgoingGroup!.textContent).toContain("future.md");
    expect(outgoingGroup!.textContent).toContain("unresolved");
  } finally {
    restore();
  }
});

test("graph page: keyboard focus advances with arrow keys and opens with Enter", async () => {
  const onOpen = vi.fn();
  const restore = installFetch(async () =>
    jsonResponse({
      "notes/alpha.md": {
        outgoing: ["notes/beta.md", "notes/gamma.md"],
        incoming: [],
        unresolved: [],
      },
      "notes/beta.md": { outgoing: [], incoming: ["notes/alpha.md"], unresolved: [] },
      "notes/gamma.md": { outgoing: [], incoming: ["notes/alpha.md"], unresolved: [] },
    }),
  );
  try {
    render(<GraphView onOpenNote={onOpen} />);
    await waitFor(() => {
      expect(screen.getByRole("application")).toBeTruthy();
    });
    const application = screen.getByRole("application");
    application.focus();

    fireEvent.keyDown(application, { key: "ArrowRight" });
    await waitFor(() => {
      // Focus badge in the header updates to show a real node label.
      const badge = document.querySelector<HTMLElement>(".graph-focus-hint");
      expect(badge?.textContent ?? "").toMatch(/alpha|beta|gamma/);
    });

    fireEvent.keyDown(application, { key: "Enter" });
    expect(onOpen).toHaveBeenCalled();
    // The opened id must match one of the fixture notes.
    expect(onOpen.mock.calls[0][0]).toMatch(/notes\/(alpha|beta|gamma)\.md/);
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

test("activity page: retry click during refresh failure preserves commits", async () => {
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) {
      return jsonResponse([
        {
          sha: "0f00d001",
          date: "2026-07-30T09:00:00+00:00",
          message: "retry-preserves-me",
          files: [{ path: "vault/notes/keep.md", status: "A" }],
        },
      ]);
    }
    throw new Error("refresh boom");
  });
  try {
    const { rerender } = render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    await waitFor(() => {
      expect(screen.getByText("retry-preserves-me")).toBeTruthy();
    });
    rerender(<ActivityFeed onOpenNote={() => {}} refreshTick={1} />);
    await waitFor(() => {
      const banner = document.querySelector<HTMLElement>(".activity-refresh-banner");
      expect(banner).toBeTruthy();
      expect(banner!.textContent).toContain("refresh boom");
    });
    // Click the in-banner Retry — the round-4 bug cleared commits here,
    // collapsing the body to the initial loading state and destroying the
    // entry the banner was meant to keep visible.
    const banner = document.querySelector<HTMLElement>(".activity-refresh-banner")!;
    const retryBtn = banner.querySelector<HTMLButtonElement>(".activity-refresh-retry")!;
    fireEvent.click(retryBtn);
    // Commit entry must still be on screen after the click; the loading
    // placeholder must not have replaced the feed body.
    expect(screen.getByText("retry-preserves-me")).toBeTruthy();
    expect(screen.queryByText(/Reading vault activity/i)).toBeNull();
  } finally {
    restore();
  }
});

test("activity page: %aI author-offset formats to viewer's local day/time", () => {
  // %aI keeps the author's UTC offset. Slicing the raw string bucketed
  // commits under the author's calendar wall-clock, so a commit authored
  // at 22:30 UTC-05:00 (= 03:30 UTC next day) appeared under 22:30 on the
  // author's day regardless of the viewer's timezone.
  const iso = "2026-07-30T22:30:00-05:00";
  const d = new Date(iso);
  const expectedTime = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  // Only a meaningful assertion when the viewer's local time differs from
  // the naive slice — skip when the environment happens to be UTC-05:00.
  if (expectedTime === "22:30") return;

  const restore = installFetch(async () =>
    jsonResponse([
      {
        sha: "aaaaaaa1234567",
        date: iso,
        message: "author-offset",
        files: [{ path: "vault/notes/tz.md", status: "A" }],
      },
    ]),
  );
  try {
    render(<ActivityFeed onOpenNote={() => {}} refreshTick={0} />);
    return waitFor(() => {
      const time = document.querySelector<HTMLElement>(".activity-time");
      expect(time).toBeTruthy();
      expect(time!.textContent).toBe(expectedTime);
      // Regression guard: the raw slice would render "22:30".
      expect(time!.textContent).not.toBe("22:30");
    });
  } finally {
    restore();
  }
});

test("tokens page: poll failure after a good snapshot keeps the chart and shows a banner", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) {
      // First good snapshot with `refreshing: true` triggers a poll.
      return jsonResponse({
        buckets: [
          {
            ts: "2026-07-30T10:00:00+00:00",
            series: { "claude/opus-4-7": { input: 4200, output: 900, cached: 50 } },
          },
        ],
        totals: { input: 4200, cached: 50, output: 900, reasoning: 0 },
        models: ["opus-4-7"],
        clis: ["claude"],
        sessions_scanned: 7,
        bucket: "hour",
        refreshing: true,
      });
    }
    throw new Error("poll boom");
  });
  try {
    render(<TokensView />);
    await waitFor(() => {
      expect(screen.getByText("4,200")).toBeTruthy();
    });
    // Advance past the 1s poll delay so the second (failing) fetch fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    await waitFor(() => {
      const banner = document.querySelector<HTMLElement>(".tokens-refresh-banner");
      expect(banner).toBeTruthy();
      expect(banner!.textContent).toContain("poll boom");
      // Chart totals from the initial snapshot must survive.
      expect(screen.getByText("4,200")).toBeTruthy();
    });
    // Regression: the full-page error state must NOT have swapped in.
    expect(screen.queryByText(/Token usage is unavailable/i)).toBeNull();
  } finally {
    restore();
    vi.useRealTimers();
  }
});

test("tokens page: clicking the banner retry preserves the current chart", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  let call = 0;
  const restore = installFetch(async () => {
    call += 1;
    if (call === 1) {
      return jsonResponse({
        buckets: [
          {
            ts: "2026-07-30T10:00:00+00:00",
            series: { "claude/opus-4-7": { input: 3300, output: 700 } },
          },
        ],
        totals: { input: 3300, cached: 0, output: 700, reasoning: 0 },
        models: ["opus-4-7"],
        clis: ["claude"],
        sessions_scanned: 4,
        bucket: "hour",
        refreshing: true,
      });
    }
    if (call === 2) throw new Error("poll boom");
    // Retry — hang so we can observe the pre-retry chart state persists.
    return new Promise<Response>(() => {});
  });
  try {
    render(<TokensView />);
    await waitFor(() => {
      expect(screen.getByText("3,300")).toBeTruthy();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1200);
    });
    let banner: HTMLElement | null = null;
    await waitFor(() => {
      banner = document.querySelector<HTMLElement>(".tokens-refresh-banner");
      expect(banner).toBeTruthy();
    });
    fireEvent.click(banner!.querySelector<HTMLButtonElement>(".tokens-refresh-retry")!);
    // Chart totals stay visible during the in-flight retry.
    expect(screen.getByText("3,300")).toBeTruthy();
    // Loading placeholder must NOT reappear.
    expect(screen.queryByText(/Reading token telemetry/i)).toBeNull();
  } finally {
    restore();
    vi.useRealTimers();
  }
});

test("health page: parseNoteUpdated interprets YYYY-MM-DD as local calendar midnight", () => {
  // Round-4 review MEDIUM: date-only frontmatter parsed as UTC midnight
  // showed same-day notes as one day old in negative offsets and shifted
  // 7d / 30d buckets by one day. The fix parses date-only as local
  // midnight — mutation-sensitive assertion below.
  const ms = parseNoteUpdated("2026-07-30");
  const d = new Date(2026, 6, 30);
  expect(ms).toBe(d.getTime());
  // Regression: Date.parse("2026-07-30") returns UTC midnight, which
  // differs from local midnight for any non-UTC viewer.
  if (d.getTimezoneOffset() !== 0) {
    expect(ms).not.toBe(Date.parse("2026-07-30"));
  }
  // Timestamped values still parse via Date.parse.
  const withTime = parseNoteUpdated("2026-07-30T15:00:00Z");
  expect(withTime).toBe(Date.parse("2026-07-30T15:00:00Z"));
});

test("health page: negative-offset same-day note reads as today, not 1d", () => {
  // Force a viewer wall-clock late in the day so a UTC-midnight parse
  // would tip over into "1d" for any non-positive offset.
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(2026, 6, 30, 22, 0, 0));
  const notes: NoteSummary[] = [
    {
      id: "n1",
      path: "vault/notes/today.md",
      title: "today",
      note_type: "reference",
      updated_at: "2026-07-30T00:00:00Z",
      meta_updated: "2026-07-30",
      tags: [],
      aliases: [],
    } as unknown as NoteSummary,
  ];
  try {
    render(
      <HealthView
        error={null}
        loading={false}
        notes={notes}
        notesLoaded
        onOpenNote={() => {}}
        onRetry={() => {}}
      />,
    );
    const age = document.querySelector<HTMLElement>(".health-age");
    expect(age?.textContent).toBe("today");
  } finally {
    vi.useRealTimers();
  }
});

test("tokens page: failed request after a preset switch does NOT keep old data under new controls", async () => {
  let call = 0;
  const seen: string[] = [];
  const restore = installFetch(async (input: RequestInfo | URL) => {
    call += 1;
    const url = String(input);
    seen.push(url);
    if (call === 1) {
      // 7d snapshot — anchor point for "old data".
      return jsonResponse({
        buckets: [
          {
            ts: "2026-07-30T10:00:00+00:00",
            series: { "claude/opus-4-7": { input: 7777, output: 111 } },
          },
        ],
        totals: { input: 7777, cached: 0, output: 111, reasoning: 0 },
        models: ["opus-4-7"],
        clis: ["claude"],
        sessions_scanned: 2,
        bucket: "hour",
        refreshing: false,
      });
    }
    // Second call fires when the preset flips to 30d — fail it.
    throw new Error("30d boom");
  });
  try {
    render(<TokensView />);
    await waitFor(() => {
      expect(screen.getByText("7,777")).toBeTruthy();
    });
    // Flip preset from 7d (default) to 30d — pickPreset() also swaps
    // bucketMode to "day", so the second request has a distinct query key.
    fireEvent.click(screen.getByRole("button", { name: /^30d$/ }));
    await waitFor(() => {
      // Full error state must render — stale 7d totals under the active
      // 30d control would misrepresent the current selection.
      expect(screen.getByRole("alert").textContent).toContain(
        "Token usage is unavailable",
      );
    });
    expect(screen.queryByText("7,777")).toBeNull();
    // Regression guard: banner (which preserves data) must NOT appear
    // when the failing request describes a different query than the
    // last-good snapshot.
    expect(document.querySelector(".tokens-refresh-banner")).toBeNull();
  } finally {
    restore();
  }
});

test("health page: calendarDayDiff returns whole-calendar-day distance regardless of DST", () => {
  // Two local Dates on consecutive calendar days should always be 1 day
  // apart in calendar terms, even when the wall-clock gap is 23h
  // (spring-forward) or 25h (fall-back) in a DST-observing timezone.
  const from = new Date(2026, 2, 8);
  const to = new Date(2026, 2, 9);
  expect(calendarDayDiff(from, to)).toBe(1);

  const fall = calendarDayDiff(new Date(2026, 10, 1), new Date(2026, 10, 2));
  expect(fall).toBe(1);

  // 7-day bucket boundary that straddles the spring transition (a naive
  // ms/86_400_000 divisor would report 6 in a spring-DST env).
  const weekAgo = new Date(2026, 2, 1);
  const now = new Date(2026, 2, 8);
  expect(calendarDayDiff(weekAgo, now)).toBe(7);

  // If the env observes DST, prove the fix is mutation-sensitive by
  // showing the naive elapsed-ms calculation would drift.
  const naive = Math.floor(
    (to.getTime() - from.getTime()) / (24 * 60 * 60 * 1000),
  );
  if (naive !== 1) {
    // Env is spring-forward DST-aware — old logic reported 0 here.
    expect(calendarDayDiff(from, to)).not.toBe(naive);
  }
});

test("health page: parseNoteUpdated stays local-midnight anchored (no regression)", () => {
  // Round-4 fix must still hold under round-5 rewrite of ageDays.
  const ms = parseNoteUpdated("2026-07-30");
  expect(ms).toBe(new Date(2026, 6, 30).getTime());
});

test("health page: date-only note keeps calendar-day age across a spring-DST bucket boundary", () => {
  // Only meaningful when the runtime observes DST. When it does, this
  // test pins wall-clock time to the moment right after spring-forward
  // and asserts a note dated exactly 7 calendar days earlier reads as
  // "7d" instead of the drifted "6d" that a fixed 24h divisor produces.
  const beforeSpring = new Date(2026, 2, 1).getTimezoneOffset();
  const afterSpring = new Date(2026, 2, 9).getTimezoneOffset();
  if (beforeSpring === afterSpring) return; // non-DST env, skip.

  vi.useFakeTimers({ shouldAdvanceTime: true });
  // 08:00 local on the day after spring-forward (US: 2026-03-09 EDT).
  vi.setSystemTime(new Date(2026, 2, 9, 8, 0, 0));
  const notes: NoteSummary[] = [
    {
      id: "n2",
      path: "vault/notes/week.md",
      title: "week",
      note_type: "reference",
      updated_at: "2026-03-02T08:00:00Z",
      meta_updated: "2026-03-02",
      tags: [],
      aliases: [],
    } as unknown as NoteSummary,
  ];
  try {
    render(
      <HealthView
        error={null}
        loading={false}
        notes={notes}
        notesLoaded
        onOpenNote={() => {}}
        onRetry={() => {}}
      />,
    );
    const age = document.querySelector<HTMLElement>(".health-age");
    expect(age?.textContent).toBe("7d");
  } finally {
    vi.useRealTimers();
  }
});
