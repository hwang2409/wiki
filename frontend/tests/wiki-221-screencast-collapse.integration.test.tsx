// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { useEffect } from "react";

import { AgentsView } from "../src/agents";
import type { AgentWorker } from "../src/api";
import { DisclosureContent } from "../src/disclosure";

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return {
    ...actual,
    getAgentModels: vi.fn().mockResolvedValue({ models: [] }),
  };
});

const worker = {
  ticket: "WIKI-1",
  registered: true,
  window: null,
  window_alive: false,
  worktree: null,
  log: null,
  orch: null,
  history: [],
  state: "working",
  pr: null,
  step: "doing things",
  blocker: null,
  status_age_seconds: 5,
  latest_event_at: null,
  latest_event_seq: null,
  last_viewed_at: null,
  last_viewed_seq: null,
  run_id: "run-1234",
  runtime_state: "running",
  control_attached: true,
  kind: "cc",
  role: "implement",
  model: "opus",
} as unknown as AgentWorker;

const data = {
  workers: [worker],
  orchestrators: [],
  archived: [],
  error: null,
};

function renderView() {
  return render(
    <AgentsView
      data={data}
      onOpenAgent={() => undefined}
      refreshTick={0}
      openTicket={null}
      onOpenTicket={() => undefined}
    />
  );
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  localStorage.clear();
  fetchMock = vi.fn(() =>
    Promise.resolve(
      new Response(JSON.stringify({ workers: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    )
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("screencast preview is collapsed by default and does not poll", async () => {
  const view = renderView();
  const toggle = view.getByRole("button", { name: /output/ });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(view.container.querySelector(".fleet-screencast")).toBeNull();
  await new Promise((resolve) => setTimeout(resolve, 50));
  const screencastCalls = fetchMock.mock.calls.filter(([input]) =>
    String(input).includes("/api/fleet/screencast")
  );
  expect(screencastCalls).toHaveLength(0);
});

test("toggle expands the preview, polls, and persists the expanded set", async () => {
  const view = renderView();
  const toggle = view.getByRole("button", { name: /output/ });
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(view.container.querySelector(".fleet-screencast")).toBeTruthy();
  expect(
    JSON.parse(localStorage.getItem("wiki-expanded-screencasts") ?? "[]")
  ).toContain("WIKI-1");
  await waitFor(() => {
    const screencastCalls = fetchMock.mock.calls.filter(([input]) =>
      String(input).includes("/api/fleet/screencast")
    );
    expect(screencastCalls.length).toBeGreaterThan(0);
  });
});

test("collapsing again unmounts the strip after the exit transition", async () => {
  const view = renderView();
  const toggle = view.getByRole("button", { name: /output/ });
  fireEvent.click(toggle);
  expect(view.container.querySelector(".fleet-screencast")).toBeTruthy();
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  // Content stays mounted through the collapse transition, then unmounts.
  await waitFor(() =>
    expect(view.container.querySelector(".fleet-screencast")).toBeNull()
  );
  expect(
    JSON.parse(localStorage.getItem("wiki-expanded-screencasts") ?? "[]")
  ).not.toContain("WIKI-1");
});

test("disclosure children stay mounted through the whole exit transition", () => {
  vi.useFakeTimers();
  try {
    const lifecycle: string[] = [];
    function Probe() {
      useEffect(() => {
        lifecycle.push("mount");
        return () => lifecycle.push("cleanup");
      }, []);
      return <div data-testid="probe" />;
    }
    const view = render(
      <DisclosureContent open>
        <Probe />
      </DisclosureContent>
    );
    expect(lifecycle).toEqual(["mount"]);
    view.rerender(
      <DisclosureContent open={false}>
        <Probe />
      </DisclosureContent>
    );
    // No cleanup in the close commit itself...
    expect(lifecycle).toEqual(["mount"]);
    expect(view.getByTestId("probe")).toBeTruthy();
    // ...nor while the 160ms grid transition runs...
    act(() => {
      vi.advanceTimersByTime(160);
    });
    expect(lifecycle).toEqual(["mount"]);
    expect(view.getByTestId("probe")).toBeTruthy();
    // ...only after the 200ms exit deadline.
    act(() => {
      vi.advanceTimersByTime(40);
    });
    expect(lifecycle).toEqual(["mount", "cleanup"]);
    expect(view.queryByTestId("probe")).toBeNull();
  } finally {
    vi.useRealTimers();
  }
});

test("reopening during the exit keeps children mounted and cancels the unmount", () => {
  vi.useFakeTimers();
  try {
    const lifecycle: string[] = [];
    function Probe() {
      useEffect(() => {
        lifecycle.push("mount");
        return () => lifecycle.push("cleanup");
      }, []);
      return <div data-testid="probe" />;
    }
    const view = render(
      <DisclosureContent open>
        <Probe />
      </DisclosureContent>
    );
    view.rerender(
      <DisclosureContent open={false}>
        <Probe />
      </DisclosureContent>
    );
    act(() => {
      vi.advanceTimersByTime(100);
    });
    view.rerender(
      <DisclosureContent open>
        <Probe />
      </DisclosureContent>
    );
    act(() => {
      vi.advanceTimersByTime(500);
    });
    expect(lifecycle).toEqual(["mount"]);
    expect(view.getByTestId("probe")).toBeTruthy();
  } finally {
    vi.useRealTimers();
  }
});

test("stored expanded state survives a reload (fresh mount)", () => {
  localStorage.setItem("wiki-expanded-screencasts", JSON.stringify(["WIKI-1"]));
  const view = renderView();
  const toggle = view.getByRole("button", { name: /output/ });
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(view.container.querySelector(".fleet-screencast")).toBeTruthy();
});
