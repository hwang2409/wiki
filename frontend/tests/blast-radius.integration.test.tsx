// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import { BlastRadiusPanel } from "../src/blast-radius";
import type { BlastRadiusPayload } from "../src/api";

const basePayload: BlastRadiusPayload = {
  candidate: "all",
  candidate_found: null,
  complete: true,
  failed_branches: [],
  branches: [],
  collisions: [],
  risk: { count: 0, level: "none", hot_files: [] },
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
