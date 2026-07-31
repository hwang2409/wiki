import { useCallback, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp, Download, Minus, Plus, RotateCcw } from "lucide-react";
import { plotInteractivity, plotPngFilename, zoomParamName, type PlotDomains, type ZoomChannel } from "../plot-interaction";
import { PlotRenderer, type PlotView } from "../artifact-renderers";

const HINT_BY_MODE: Record<string, string> = {
  full: "Drag to pan · wheel to zoom · shift-drag to select · toolbar buttons also work by keyboard",
  tooltip: "Hover for values",
  static: "Static plot",
};

export type KeyboardAction = "zoom-in" | "zoom-out" | "pan-left" | "pan-right" | "pan-up" | "pan-down";

function scaleDomain(view: PlotView, channel: ZoomChannel): [number, number] | null {
  const values = view.scale?.(channel).domain() ?? [];
  if (values.length !== 2) return null;
  const low = Number(values[0]);
  const high = Number(values[1]);
  if (!Number.isFinite(low) || !Number.isFinite(high) || low === high) return null;
  return [low, high];
}

function scaleAnchor(view: PlotView, channel: ZoomChannel, domain: [number, number]): number {
  const scale = view.scale?.(channel);
  const range = scale?.range?.() ?? [];
  if (range.length === 2 && scale?.invert) {
    const first = Number(range[0]);
    const second = Number(range[1]);
    const anchor = Number(scale.invert((first + second) / 2));
    if (Number.isFinite(anchor)) return anchor;
  }
  return (domain[0] + domain[1]) / 2;
}

export function applyKeyboardControl(
  view: PlotView,
  channels: readonly ZoomChannel[],
  action: KeyboardAction,
): boolean {
  if (!view.signal || !view.run) return false;
  const isZoom = action === "zoom-in" || action === "zoom-out";
  let changed = false;
  for (const channel of channels) {
    const domain = scaleDomain(view, channel);
    if (!domain) continue;
    const param = zoomParamName(channel);
    if (isZoom) {
      const anchor = scaleAnchor(view, channel, domain);
      const factor = action === "zoom-in" ? 0.8 : 1 / 0.8;
      view.signal(`${param}_zoom_anchor`, { x: anchor, y: anchor });
      view.signal(`${param}_zoom_delta`, factor);
      changed = true;
      continue;
    }
    const width = view.width?.() ?? 0;
    const height = view.height?.() ?? 0;
    const delta = { x: 0, y: 0 };
    if (channel === "x" && width > 0 && (action === "pan-left" || action === "pan-right")) {
      delta.x = width * (action === "pan-left" ? -0.2 : 0.2);
    } else if (channel === "y" && height > 0 && (action === "pan-up" || action === "pan-down")) {
      delta.y = height * (action === "pan-up" ? -0.2 : 0.2);
    } else {
      continue;
    }
    view.signal(`${param}_translate_anchor`, {
      x: 0,
      y: 0,
      extent_x: domain,
      extent_y: domain,
    });
    view.signal(`${param}_translate_delta`, delta);
    changed = true;
  }
  if (!changed) return false;
  view.run();
  return true;
}

export function PlotArtifactDetail({ spec, title }: { spec: Record<string, unknown>; title?: string | null }) {
  const [domains, setDomains] = useState<PlotDomains | null>(null);
  const [renderKey, setRenderKey] = useState(0);
  const [viewReady, setViewReady] = useState(false);
  const [exportStatus, setExportStatus] = useState<string | null>(null);
  const viewRef = useRef<PlotView | null>(null);
  const interactivity = useMemo(() => plotInteractivity(spec), [spec]);
  const canInteract = interactivity.mode === "full";

  const onView = useCallback((view: PlotView | null) => {
    viewRef.current = view;
    setViewReady(view !== null);
    if (view) setExportStatus(null);
  }, []);
  const onBrush = useCallback((next: PlotDomains) => {
    const merged: PlotDomains = { ...(domains ?? {}), ...next };
    if (viewRef.current) {
      for (const channel of interactivity.mode === "full" ? interactivity.channels : []) {
        if (next[channel]) continue;
        const live = scaleDomain(viewRef.current, channel);
        if (live) merged[channel] = live;
      }
    }
    setDomains(merged);
  }, [domains, interactivity]);
  // Reset is always enabled in full mode: pan / wheel-zoom mutate Vega's
  // internal scales without touching React state, so `domains === null` is not
  // proof that the plot is at its default view. Bumping renderKey forces a
  // re-embed which resets Vega too.
  const onReset = useCallback(() => {
    setDomains(null);
    setRenderKey((key) => key + 1);
  }, []);
  const onKeyboardControl = useCallback((action: KeyboardAction) => {
    const view = viewRef.current;
    if (!view || !canInteract) return;
    applyKeyboardControl(view, interactivity.channels, action);
  }, [canInteract, interactivity]);
  const interactiveChannels = interactivity.mode === "full" ? interactivity.channels : [];
  const hasXChannel = interactiveChannels.includes("x");
  const hasYChannel = interactiveChannels.includes("y");
  const onSavePng = useCallback(async () => {
    const view = viewRef.current;
    if (!view) {
      setExportStatus("PNG export is not ready.");
      return;
    }
    try {
      const url = await view.toImageURL("png", 2);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = plotPngFilename(title);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setExportStatus("PNG saved.");
    } catch {
      setExportStatus("PNG export failed. Try again when the plot is ready.");
    }
  }, [title]);

  return (
    <div className="artifact-plot-detail">
      <div className="artifact-detail-toolbar">
        <button
          type="button"
          onClick={onReset}
          disabled={!canInteract}
          aria-label="Reset zoom"
          data-panel-reset-zoom
        >
          <RotateCcw size={12} /> Reset zoom
        </button>
        {canInteract ? (
          <>
            <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("zoom-in")} aria-label="Zoom in" title="Zoom in">
              <Plus size={12} /> Zoom in
            </button>
            <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("zoom-out")} aria-label="Zoom out" title="Zoom out">
              <Minus size={12} /> Zoom out
            </button>
            {hasXChannel ? (
              <>
                <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("pan-left")} aria-label="Pan left" title="Pan left">
                  <ArrowLeft size={12} /> Pan left
                </button>
                <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("pan-right")} aria-label="Pan right" title="Pan right">
                  <ArrowRight size={12} /> Pan right
                </button>
              </>
            ) : null}
            {hasYChannel ? (
              <>
                <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("pan-up")} aria-label="Pan up" title="Pan up">
                  <ArrowUp size={12} /> Pan up
                </button>
                <button type="button" disabled={!viewReady} onClick={() => onKeyboardControl("pan-down")} aria-label="Pan down" title="Pan down">
                  <ArrowDown size={12} /> Pan down
                </button>
              </>
            ) : null}
          </>
        ) : null}
        <button type="button" disabled={!viewReady} onClick={() => void onSavePng()} aria-label="Save as PNG">
          <Download size={12} /> Save PNG
        </button>
        <span aria-live="polite" className="artifact-plot-status" data-testid="plot-export-status" role="status">{exportStatus}</span>
        <span className="artifact-plot-hint">{HINT_BY_MODE[interactivity.mode] ?? ""}</span>
      </div>
      <div
        className="artifact-plot-detail-canvas"
        data-interactive={canInteract ? "true" : undefined}
        onDoubleClick={canInteract ? onReset : undefined}
      >
        <PlotRenderer
          key={renderKey}
          spec={spec}
          actions
          interactive={canInteract}
          domains={domains ?? undefined}
          onBrush={onBrush}
          onView={onView}
        />
      </div>
    </div>
  );
}
