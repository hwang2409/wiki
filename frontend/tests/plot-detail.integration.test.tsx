// @vitest-environment jsdom
import { useEffect } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

type MockView = {
  toImageURL: () => Promise<string>;
};

vi.mock("../src/artifact-renderers", () => ({
  PlotRenderer: ({ onView }: { onView?: (view: MockView | null) => void }) => {
    useEffect(() => {
      const view: MockView = {
        toImageURL: async () => {
          throw new Error("PNG encoder unavailable");
        },
      };
      const timer = window.setTimeout(() => onView?.(view), 0);
      return () => {
        window.clearTimeout(timer);
        onView?.(null);
      };
    }, [onView]);
    return <div data-testid="mock-plot" />;
  },
}));

import { PlotArtifactDetail } from "../src/artifact-detail/plot";

const FULL_SPEC = {
  mark: "point",
  encoding: {
    x: { field: "x", type: "quantitative" },
    y: { field: "y", type: "quantitative" },
  },
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("PlotArtifactDetail export feedback", () => {
  test("disables PNG export until ready and reports export failure", async () => {
    render(<PlotArtifactDetail spec={FULL_SPEC} title="Failure fixture" />);
    const save = screen.getByRole("button", { name: "Save as PNG" });
    expect((save as HTMLButtonElement).disabled).toBe(true);
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(save);
    await waitFor(() => {
      const status = screen.getByRole("status");
      expect(status.textContent).toMatch(/PNG export failed/i);
    });
  });
});
