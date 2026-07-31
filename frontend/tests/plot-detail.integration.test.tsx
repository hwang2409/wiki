// @vitest-environment jsdom
import { useEffect } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

type MockView = {
  toImageURL: () => Promise<string>;
  scale: (channel: string) => { domain: () => number[] };
};

vi.mock("../src/artifact-renderers", () => ({
  PlotRenderer: ({
    domains,
    onBrush,
    onView,
  }: {
    domains?: Record<string, unknown>;
    onBrush?: (domains: Record<string, [number, number]>) => void;
    onView?: (view: MockView | null) => void;
  }) => {
    useEffect(() => {
      const view: MockView = {
        toImageURL: async () => {
          throw new Error("PNG encoder unavailable");
        },
        scale: (channel) => ({ domain: () => channel === "y" ? [20, 40] : [0, 10] }),
      };
      const timer = window.setTimeout(() => onView?.(view), 0);
      return () => {
        window.clearTimeout(timer);
        onView?.(null);
      };
    }, [domains, onView]);
    return (
      <>
        <div data-testid="mock-plot" />
        <button type="button" data-testid="emit-brush" onClick={() => onBrush?.({ x: [2, 4] })}>
          Emit brush
        </button>
        <output data-testid="plot-domains">{JSON.stringify(domains ?? null)}</output>
      </>
    );
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

const X_ONLY_SPEC = {
  mark: "point",
  encoding: {
    x: { field: "x", type: "quantitative" },
    y: { field: "group", type: "nominal" },
  },
};

const Y_ONLY_SPEC = {
  mark: "point",
  encoding: {
    x: { field: "group", type: "nominal" },
    y: { field: "y", type: "quantitative" },
  },
};

const SAME_FIELD_SPEC = {
  mark: "point",
  encoding: {
    x: { field: "value", type: "quantitative" },
    y: { field: "value", type: "quantitative" },
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
      const status = screen.getByTestId("plot-export-status");
      expect(status.textContent).toMatch(/PNG export failed/i);
    });
  });
});

describe("PlotArtifactDetail keyboard controls", () => {
  test("same-field x brush keeps the live y zoom in the re-embed domains", async () => {
    render(<PlotArtifactDetail spec={SAME_FIELD_SPEC} title="Same field" />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(screen.getByTestId("emit-brush"));
    await waitFor(() => {
      const domains = screen.getByTestId("plot-domains").textContent ?? "";
      expect(domains).toContain('"x":[2,4]');
      expect(domains).toContain('"y":[20,40]');
    });
  });

  test("x-only plots show horizontal pan controls only", async () => {
    render(<PlotArtifactDetail spec={X_ONLY_SPEC} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByRole("button", { name: "Pan left" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pan up" })).toBeNull();
  });

  test("y-only plots show vertical pan controls only", async () => {
    render(<PlotArtifactDetail spec={Y_ONLY_SPEC} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.getByRole("button", { name: "Pan up" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Pan left" })).toBeNull();
  });
});
