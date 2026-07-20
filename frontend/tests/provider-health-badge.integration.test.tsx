// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";

import type { ProviderHealthSnapshot } from "../src/api";
import { ProviderHealthBadge } from "../src/provider-health-badge";

afterEach(() => {
  cleanup();
});

test("renders nothing when every provider is ok", async () => {
  const fetchHealth = vi.fn(
    async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => ({
      cdx: { status: "ok", checked_at: "t0", detail: null },
      cc: { status: "ok", checked_at: "t0", detail: null },
    })
  );
  const { container } = render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);
  await waitFor(() => expect(fetchHealth).toHaveBeenCalled());
  // Nothing to render for a healthy fleet — badge container is absent.
  expect(container.querySelector("[data-testid='provider-health-badges']")).toBeNull();
});

test("shows only the unhealthy providers with tooltip detail", async () => {
  const fetchHealth = vi.fn(
    async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => ({
      cdx: {
        status: "unauthorized",
        checked_at: "2026-07-20T00:00:00Z",
        detail: "codex reported access token could not be refreshed",
      },
      cc: { status: "ok", checked_at: "t0", detail: null },
    })
  );
  render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);

  const badge = await screen.findByText("cdx auth dead");
  expect(badge).toBeTruthy();
  expect(badge.getAttribute("title") || "").toContain("could not be refreshed");
  expect(badge.getAttribute("data-provider")).toBe("cdx");
  // Healthy provider must not render its own badge.
  expect(screen.queryByText("cc auth dead")).toBeNull();
});

test("unknown status is treated as healthy (no badge)", async () => {
  const fetchHealth = vi.fn(
    async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => ({
      cdx: { status: "unknown", checked_at: "t0", detail: "no auth.json yet" },
      cc: { status: "unknown", checked_at: "t0", detail: null },
    })
  );
  const { container } = render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);
  await waitFor(() => expect(fetchHealth).toHaveBeenCalled());
  expect(container.querySelector("[data-testid='provider-health-badges']")).toBeNull();
});
