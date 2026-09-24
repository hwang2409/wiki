// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

const sessionTab = vi.hoisted(() => vi.fn(({ archivedAt, runId }: { archivedAt?: string; runId?: string }) => (
  <div data-testid="session-target">{archivedAt ?? "live"}:{runId ?? "live"}</div>
)));

vi.mock("../src/session", () => ({
  SessionTab: sessionTab,
  InspectableSessionTab: () => null,
  usePollTick: () => 0,
}));
vi.mock("../src/artifact-inspector", () => ({
  useArtifactInspector: () => ({
    handleArtifactsChange: () => {},
    inspector: null,
    inspectorOpen: false,
    openInspector: () => {},
  }),
}));
vi.mock("../src/artifact-panel", () => ({ ArtifactPanel: () => <div>artifact panel</div> }));

import { AgentSessionView } from "../src/agent-session-surface";

afterEach(() => {
  cleanup();
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  sessionTab.mockClear();
});

test("artifact deep link opens the saved receipt without live run actions", () => {
  window.history.replaceState(
    null,
    "",
    "/?panel=WIKI-1&artifact=saved&tab=saved&focus=saved&archived_at=2026-08-18T00%3A00%3A00%2B00%3A00&run_id=saved-run#/agent/WIKI-1",
  );

  render(<AgentSessionView refreshTick={0} worker={{ ticket: "WIKI-1", kind: "cdx", model: "sol", canReplace: true }} />);

  expect(screen.getByTestId("session-target").textContent).toBe("2026-08-18T00:00:00+00:00:saved-run");
  expect(sessionTab.mock.lastCall?.[0]).toMatchObject({ showComposer: false });
  expect(screen.getByText("saved artifacts")).toBeTruthy();
  expect(screen.queryByText("Replace")).toBeNull();
  expect(screen.queryByText("Graph")).toBeNull();

  act(() => {
    window.history.replaceState(
      null,
      "",
      "/?panel=WIKI-1&artifact=other&tab=other&focus=other&archived_at=2026-08-19T00%3A00%3A00%2B00%3A00&run_id=other-run#/agent/WIKI-1",
    );
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  expect(screen.getByTestId("session-target").textContent).toBe("2026-08-19T00:00:00+00:00:other-run");
});
