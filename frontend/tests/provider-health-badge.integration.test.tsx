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
      cdx: { status: "ok", checked_at: "t0", reason_code: null },
      cc: { status: "ok", checked_at: "t0", reason_code: null },
    })
  );
  const { container } = render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);
  await waitFor(() => expect(fetchHealth).toHaveBeenCalled());
  // Nothing to render for a healthy fleet — badge container is absent.
  expect(container.querySelector("[data-testid='provider-health-badges']")).toBeNull();
});

test("shows only the unauthorized providers with fixed remediation", async () => {
  const fetchHealth = vi.fn(
    async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => ({
      cdx: {
        status: "unauthorized",
        checked_at: "2026-07-20T00:00:00Z",
        reason_code: "auth_dead",
      },
      cc: { status: "ok", checked_at: "t0", reason_code: null },
    })
  );
  render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);

  const badge = await screen.findByText("cdx auth unavailable");
  expect(badge).toBeTruthy();
  expect(badge.getAttribute("title") || "").toContain("codex login");
  expect(badge.getAttribute("data-provider")).toBe("cdx");
  // Healthy provider must not render its own badge.
  expect(screen.queryByText("cc auth unavailable")).toBeNull();
});

test("unknown status renders a distinct neutral accessible badge", async () => {
  const fetchHealth = vi.fn(
    async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => ({
      cdx: { status: "unknown", checked_at: "t0", reason_code: "credentials_missing" },
      cc: { status: "unknown", checked_at: "t0", reason_code: "verification_unavailable" },
    })
  );
  render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);
  const badges = await screen.findByTestId("provider-health-badges");
  expect(badges.getAttribute("role")).toBe("status");
  expect(screen.getByText("cdx auth status unknown")).toBeTruthy();
  expect(screen.getByText("cc auth status unknown")).toBeTruthy();
  expect(badges.querySelector(".is-unknown")).toBeTruthy();
});

test("probing status is distinct from unknown", () => {
  const fetchHealth = vi.fn(() => new Promise<ProviderHealthSnapshot>(() => {}));
  render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);
  const badges = screen.getByTestId("provider-health-badges");
  expect(badges.getAttribute("data-state")).toBe("probing");
  expect(screen.getByText("checking provider auth")).toBeTruthy();
  expect(badges.querySelector(".is-probing")).toBeTruthy();
});

test("initial request failure renders a fixed neutral error state", async () => {
  const fetchHealth = vi.fn(async (_signal: AbortSignal): Promise<ProviderHealthSnapshot> => {
    throw new Error("raw token /Users/secret/auth.json");
  });
  render(<ProviderHealthBadge fetchHealth={fetchHealth} pollMs={60_000} />);

  const badges = await screen.findByTestId("provider-health-badges");
  expect(badges.getAttribute("data-state")).toBe("error");
  expect(badges.getAttribute("role")).toBe("status");
  expect(screen.getByText("provider auth unavailable")).toBeTruthy();
  expect(badges.querySelector(".is-error")).toBeTruthy();
  expect(screen.queryByText("checking provider auth")).toBeNull();
  expect(badges.textContent).not.toContain("raw token");
  expect(badges.textContent).not.toContain("auth.json");
});
