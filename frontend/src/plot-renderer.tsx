import { useEffect, useRef, useState } from "react";
import { ArtifactError, ArtifactPlaceholder } from "./artifact-state";
import {
  BRUSH_TUPLE_SIGNAL,
  buildInteractiveSpec,
  makeBrushBuffer,
  plotInteractivity,
  type PlotDomains,
  type PlotDomainDirection,
  type ZoomChannel,
} from "./plot-interaction";
import { useCurrentTheme } from "./shiki";

export type PlotView = {
  toImageURL: (type: string, scaleFactor?: number) => Promise<string>;
  scale?: (name: string) => {
    domain: () => unknown[];
    invert?: (value: number) => unknown;
    range?: () => unknown[];
  };
  signal?: (name: string, value?: unknown) => PlotView;
  run?: () => PlotView;
  width?: () => number;
  height?: () => number;
};

// Attached to the plot container element so browser tests (and devtools
// debugging) can reach the underlying Vega view. Not exported anywhere; the
// property name is deliberate and stable so external tools can rely on it.
type PlotContainerElement = HTMLDivElement & { __wikiVegaView?: unknown };

export type PlotRendererProps = {
  actions?: boolean;
  domains?: PlotDomains;
  interactive?: boolean;
  onBrush?: (domains: PlotDomains) => void;
  onView?: (view: PlotView | null) => void;
  spec: Record<string, unknown>;
};

export function PlotRenderer({
  actions = false,
  domains,
  interactive = false,
  onBrush,
  onView,
  spec,
}: PlotRendererProps) {
  const theme = useCurrentTheme();
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [nonce, setNonce] = useState(0);
  const brushRef = useRef(onBrush);
  const viewRef = useRef(onView);
  useEffect(() => { brushRef.current = onBrush; }, [onBrush]);
  useEffect(() => { viewRef.current = onView; }, [onView]);
  useEffect(() => {
    setReady(false);
    const target = container.current;
    if (!target) return;
    let finalized = false;
    let finalize: (() => void) | undefined;
    let brushCleanup: (() => void) | undefined;
    const styles = getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue("--text-normal").trim();
    const muted = styles.getPropertyValue("--text-muted").trim();
    const border = styles.getPropertyValue("--background-modifier-border").trim();
    const background = styles.getPropertyValue("--background-primary").trim();
    const accent = styles.getPropertyValue("--accent-primary").trim();
    const font = styles.getPropertyValue("--font-monospace").trim();
    const interactivity = plotInteractivity(spec);
    const interactiveSpec = buildInteractiveSpec(spec, {
      interactivity,
      armed: interactive,
      domains,
      brushColor: accent,
    });
    const sourceConfig: Record<string, unknown> =
      typeof interactiveSpec.config === "object" && interactiveSpec.config
        ? interactiveSpec.config as Record<string, unknown>
        : {};
    const sourceAxis = typeof sourceConfig.axis === "object" && sourceConfig.axis ? sourceConfig.axis : {};
    const sourceLegend = typeof sourceConfig.legend === "object" && sourceConfig.legend ? sourceConfig.legend : {};
    const sourceTitle = typeof sourceConfig.title === "object" && sourceConfig.title ? sourceConfig.title : {};
    const sourceRange = typeof sourceConfig.range === "object" && sourceConfig.range ? sourceConfig.range : {};
    const themedSpec = {
      ...interactiveSpec,
      background,
      config: {
        ...sourceConfig,
        font,
        background,
        axis: { ...sourceAxis, domainColor: border, gridColor: border, labelColor: muted, titleColor: text },
        legend: { ...sourceLegend, labelColor: muted, titleColor: text },
        title: { ...sourceTitle, color: text, font },
        range: { ...sourceRange, category: [accent, text, muted, border] },
      },
    };
    const reportPlotFailure = (reason: unknown) => {
      if (finalized) return;
      setError(reason instanceof Error ? reason.message : "Plot could not render this spec.");
    };
    import("vega-embed").then(async ({ default: embed }) => {
      try {
        const result = await embed(target, themedSpec, { actions, renderer: "svg" });
        finalize = result.finalize;
        if (finalized) {
          result.finalize?.();
          return;
        }
        setError(null);
        setReady(true);
        (target as PlotContainerElement).__wikiVegaView = result.view;
        viewRef.current?.(result.view as PlotView);
        if (interactive && interactivity.mode === "full") {
          const buffer = makeBrushBuffer(
            interactivity.channels,
            (domainsFromBrush) => brushRef.current?.(domainsFromBrush),
            (channel: ZoomChannel): PlotDomainDirection | undefined => {
              try {
                const domain = result.view.scale(channel).domain();
                const first = Number(domain[0]);
                const second = Number(domain[1]);
                if (!Number.isFinite(first) || !Number.isFinite(second) || first === second) return undefined;
                return first > second ? "descending" : "ascending";
              } catch {
                return undefined;
              }
            },
          );
          try {
            // Listen to the compiled tuple signal (channel-tagged) so the
            // reader is agnostic to nested field paths / escaped keys /
            // same-field axes — metadata carries the channel, not the map key.
            result.view.addSignalListener(BRUSH_TUPLE_SIGNAL, (_name, value) => buffer.onSignal(value));
          } catch { /* Vega drops listeners if user spec stripped the param */ }
          window.addEventListener("pointerup", buffer.onPointerUp);
          window.addEventListener("pointercancel", buffer.onCancel);
          brushCleanup = () => {
            window.removeEventListener("pointerup", buffer.onPointerUp);
            window.removeEventListener("pointercancel", buffer.onCancel);
          };
        }
      } catch (reason) {
        reportPlotFailure(reason);
      }
    }).catch(reportPlotFailure);
    return () => {
      finalized = true;
      viewRef.current?.(null);
      brushCleanup?.();
      finalize?.();
      delete (target as PlotContainerElement).__wikiVegaView;
      target.replaceChildren();
    };
  }, [actions, nonce, spec, theme, interactive, domains]);
  if (error) {
    return (
      <ArtifactError
        detail={error}
        onRetry={() => {
          setError(null);
          setNonce((value) => value + 1);
        }}
        title="Plot couldn’t render."
      />
    );
  }
  return (
    <div className="artifact-plot-wrap">
      {!ready ? <ArtifactPlaceholder label="Rendering plot…" shape="plot" /> : null}
      <div className="artifact-plot" ref={container} style={ready ? undefined : { visibility: "hidden", position: "absolute" }} />
    </div>
  );
}
