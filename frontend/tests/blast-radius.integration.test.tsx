// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { BlastRadiusPanel, type BlastRadiusPanelProps } from "../src/blast-radius";
import type { BlastRadiusPayload } from "../src/api";

const basePayload: BlastRadiusPayload = {
  candidate: "all",
  candidate_found: null,
  complete: true,
  failed_branches: [],
  branches: [],
  collisions: [],
  risk: { count: 0, level: "none", hot_files: [] },
  refreshed_at: Date.now() / 1000,
  snapshot_max_age_seconds: 90,
};

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

test("renders no overlap only after a complete analysis", async () => {
  const fetchData = vi.fn(async () => basePayload);
  render(<BlastRadiusPanel fetchData={fetchData} refreshMs={60_000} />);

  expect(await screen.findByTestId("blast-radius-no-overlap")).toBeTruthy();
  expect(screen.queryByTestId("blast-radius-incomplete")).toBeNull();
});

test("renders failed branches instead of a false no-overlap state", async () => {
  const fetchData = vi.fn(async (): Promise<BlastRadiusPayload> => ({
    ...basePayload,
    complete: false,
    failed_branches: [{ branch: "deleted", reason: "branch ref is not present" }],
  }));
  render(<BlastRadiusPanel fetchData={fetchData} refreshMs={60_000} />);

  expect(await screen.findByTestId("blast-radius-incomplete")).toBeTruthy();
  expect(screen.getByText("deleted")).toBeTruthy();
  expect(screen.queryByTestId("blast-radius-no-overlap")).toBeNull();
});

test("renders unknown risk without a zero-collision claim", async () => {
  const fetchData = vi.fn(async (): Promise<BlastRadiusPayload> => ({
    ...basePayload,
    candidate_found: false,
    complete: false,
    risk: null,
    failed_branches: [{ branch: "deleted", reason: "candidate branch was not found" }],
  }));
  render(<BlastRadiusPanel fetchData={fetchData} refreshMs={60_000} />);

  expect(await screen.findByTestId("blast-radius-incomplete")).toBeTruthy();
  expect(screen.queryByText("0 collisions")).toBeNull();
  expect(screen.queryByTestId("blast-radius-no-overlap")).toBeNull();
});

test("marks the last result stale after a polling error", async () => {
  vi.useFakeTimers();
  const fetchData = vi
    .fn<NonNullable<BlastRadiusPanelProps["fetchData"]>>()
    .mockResolvedValueOnce(basePayload)
    .mockRejectedValueOnce(new Error("network offline"));
  render(<BlastRadiusPanel fetchData={fetchData} refreshMs={1_000} />);

  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(screen.getByTestId("blast-radius-no-overlap")).toBeTruthy();
  await act(async () => {
    await vi.advanceTimersByTimeAsync(1_000);
  });

  expect(screen.getByTestId("blast-radius-incomplete")).toBeTruthy();
  expect(screen.getByText(/results stale/)).toBeTruthy();
  expect(screen.queryByText("0 collisions")).toBeNull();
  expect(screen.queryByTestId("blast-radius-no-overlap")).toBeNull();
});
