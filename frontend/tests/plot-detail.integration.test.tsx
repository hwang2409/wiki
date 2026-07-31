// @vitest-environment jsdom
import { useEffect, useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

type MockView = {
  toImageURL: () => Promise<string>;
  scale: (channel: string) => { domain: () => number[] };
  scaleNames?: { x?: string; y?: string };
  signal: (name: string, value: unknown) => MockView;
  run: () => MockView;
};

let mockLiveDomain: [number, number] = [0, 10];
let mockScaleCalls: string[] = [];

vi.mock("../src/artifact-renderers", () => ({
  PlotRenderer: ({
    domains,
    onBrush,
    onView,
    spec,
  }: {
    domains?: Record<string, unknown>;
    onBrush?: (domains: Record<string, [number, number]>) => void;
    onView?: (view: MockView | null) => void;
    spec?: Record<string, unknown>;
  }) => {
    const [, rerender] = useState(0);
    useEffect(() => {
      mockLiveDomain = [0, 10];
      mockScaleCalls = [];
      const view: MockView = {
        toImageURL: async () => {
          throw new Error("PNG encoder unavailable");
        },
        scaleNames: typeof spec?.name === "string" ? { x: "named_unit_x", y: "named_unit_y" } : undefined,
        scale: (channel) => {
          mockScaleCalls.push(channel);
          return { domain: () => channel === "y" || channel === "named_unit_y" ? [20, 40] : mockLiveDomain };
        },
        signal: (_name, value) => {
          if (typeof value === "number" && value < 1) mockLiveDomain = [1, 9];
          return view;
        },
        run: () => view,
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
        <button
          type="button"
          data-testid="mutate-user-scale"
          onClick={() => {
            mockLiveDomain = [2, 8];
            rerender((value) => value + 1);
          }}
        >
          Mutate user scale
        </button>
        <output data-testid="mock-live-domain">{JSON.stringify(mockLiveDomain)}</output>
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

const SHARED_ENCODING_FACET_SPEC = {
  mark: "point",
  data: { values: [{ x: 1, y: 1, facet: "a" }, { x: 2, y: 2, facet: "b" }] },
  encoding: {
    facet: { field: "facet", type: "nominal" },
    x: { field: "x", type: "quantitative" },
    y: { field: "y", type: "quantitative" },
  },
  resolve: { scale: { x: "shared", y: "shared" } },
};

const INDEPENDENT_ENCODING_FACET_SPEC = {
  ...SHARED_ENCODING_FACET_SPEC,
  resolve: { scale: { x: "independent", y: "independent" } },
};

const SAME_FIELD_SPEC = {
  mark: "point",
  encoding: {
    x: { field: "value", type: "quantitative" },
    y: { field: "value", type: "quantitative" },
  },
};

const USER_SCALE_BINDING_SPEC = {
  ...FULL_SPEC,
  params: [{ name: "user_pan", select: { type: "interval" }, bind: "scales" }],
};

const NAMED_UNIT_SPEC = {
  ...FULL_SPEC,
  name: "named-unit",
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  mockScaleCalls = [];
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

  test.each([
    ["shared", SHARED_ENCODING_FACET_SPEC],
    ["independent", INDEPENDENT_ENCODING_FACET_SPEC],
  ])("%s encoding.facet plots show no toolbar controls", async (_label, spec) => {
    render(<PlotArtifactDetail spec={spec} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("button", { name: "Zoom in" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Zoom out" })).toBeNull();
  });

  test("preserved user scale bindings keep Reset zoom available", async () => {
    render(<PlotArtifactDetail spec={USER_SCALE_BINDING_SPEC} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));
    const reset = screen.getByRole("button", { name: "Reset zoom" });
    expect((reset as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByTestId("mutate-user-scale"));
    await waitFor(() => expect(screen.getByTestId("mock-live-domain").textContent).toBe("[2,8]"));
    fireEvent.click(reset);
    await waitFor(() => expect(screen.getByTestId("mock-live-domain").textContent).toBe("[0,10]"));
  });

  test("named units use normalized scales for toolbar zoom and partial brushes", async () => {
    render(<PlotArtifactDetail spec={NAMED_UNIT_SPEC} />);
    await waitFor(() => expect((screen.getByRole("button", { name: "Save as PNG" }) as HTMLButtonElement).disabled).toBe(false));

    fireEvent.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(mockScaleCalls).toContain("named_unit_x");
    expect(mockScaleCalls).toContain("named_unit_y");

    fireEvent.click(screen.getByTestId("emit-brush"));
    await waitFor(() => {
      const domains = screen.getByTestId("plot-domains").textContent ?? "";
      expect(domains).toContain('"x":[2,4]');
      expect(domains).toContain('"y":[20,40]');
    });
  });
});
