import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CommandPalette } from "../src/command-palette";
import { searchPalette } from "../src/api";

vi.mock("../src/api", async () => {
  const actual = await vi.importActual<typeof import("../src/api")>("../src/api");
  return { ...actual, searchPalette: vi.fn() };
});

const mockedSearchPalette = vi.mocked(searchPalette);

const lexical = {
  kind: "note" as const,
  id: "lexical.md",
  title: "lexical note",
  subtitle: "literal match",
  url: "#/note/lexical.md",
  updated_at: null,
  score: 1,
};

const semantic = {
  kind: "note" as const,
  id: "semantic:meaning.md",
  title: "semantic note",
  subtitle: "related context",
  url: "#/note/meaning.md",
  updated_at: null,
  score: 0.92,
};

describe("command palette search modes", () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    cleanup();
    mockedSearchPalette.mockReset();
  });

  it("keeps lexical mode as the default and separates semantic results", async () => {
    mockedSearchPalette.mockImplementation(async (_query, _limit, _signal, mode) =>
      mode === "semantic"
        ? {
            mode,
            results: [lexical],
            lexical_results: [lexical],
            semantic_results: [semantic],
            semantic_available: true,
          }
        : { mode: "lexical", results: [lexical] },
    );

    render(<CommandPalette onClose={() => undefined} onOpen={() => undefined} />);
    await waitFor(() => expect(screen.getByRole("option", { name: /lexical note/ })).toBeTruthy());
    expect(screen.getByRole("button", { name: "lexical", pressed: true })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "semantic" }));
    await waitFor(() => expect(screen.getByRole("option", { name: /semantic note/ })).toBeTruthy());
    expect(screen.getByText("Lexical matches")).toBeTruthy();
    expect(screen.getByText("Semantic matches")).toBeTruthy();
    expect(screen.getByRole("button", { name: "semantic", pressed: true })).toBeTruthy();
  });

  it("shows a clear unavailable state while retaining lexical matches", async () => {
    mockedSearchPalette.mockResolvedValue({
      mode: "semantic",
      results: [lexical],
      lexical_results: [lexical],
      semantic_results: [],
      semantic_available: false,
      semantic_unavailable_reason: "semantic search unavailable: no embedding API key configured",
    });

    render(<CommandPalette onClose={() => undefined} onOpen={() => undefined} />);
    fireEvent.click(screen.getByRole("button", { name: "semantic" }));
    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("no embedding API key configured"),
    );
    expect(screen.getByRole("option", { name: /lexical note/ })).toBeTruthy();
  });
});
