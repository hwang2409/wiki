// @vitest-environment jsdom
// WIKI-200 — the entrypoint must apply the backend vault identity.

import { afterEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createRoot: vi.fn(() => ({ render: vi.fn() })),
  setThumbnailCacheNamespace: vi.fn(),
}));

vi.mock("react-dom/client", () => ({ createRoot: mocks.createRoot }));
vi.mock("../src/App", () => ({ default: () => null }));
vi.mock("../src/external-links", () => ({ installExternalLinkInterceptors: vi.fn() }));
vi.mock("../src/lowercase-mode", () => ({ applyStoredLowercase: vi.fn() }));
vi.mock("../src/settings", () => ({ applyStoredFonts: vi.fn() }));
vi.mock("../src/themes", () => ({ applyStoredTheme: vi.fn() }));
vi.mock("../src/thumbnail-cache", () => ({
  setThumbnailCacheNamespace: mocks.setThumbnailCacheNamespace,
}));
vi.mock("../src/ui-state-sync", () => ({
  hydrateFromServer: vi.fn().mockResolvedValue(undefined),
  installUiStateWriteBack: vi.fn(),
}));

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("wiki bootstrap", () => {
  test("applies the backend vault identity through the production bootstrap", async () => {
    document.body.innerHTML = '<div id="root"></div>';
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ identity: "vault-test" }), { status: 200 }),
    ));

    await import("../src/main");

    await vi.waitFor(() => {
      expect(mocks.setThumbnailCacheNamespace).toHaveBeenCalledWith("vault-test");
    });
  });
});
