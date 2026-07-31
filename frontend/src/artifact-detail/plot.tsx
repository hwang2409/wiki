import { useCallback, useMemo, useRef, useState } from "react";
import { Download, RotateCcw } from "lucide-react";
import { plotInteractivity, plotPngFilename, type PlotDomains } from "../plot-interaction";
import { PlotRenderer, type PlotView } from "../artifact-renderers";

const HINT_BY_MODE: Record<string, string> = {
  full: "Drag to pan · wheel to zoom · shift-drag to select · double-click to reset",
  tooltip: "Hover for values",
  static: "Static plot",
};

export function PlotArtifactDetail({ spec, title }: { spec: Record<string, unknown>; title?: string | null }) {
  const [domains, setDomains] = useState<PlotDomains | null>(null);
  const [renderKey, setRenderKey] = useState(0);
  const viewRef = useRef<PlotView | null>(null);
  const interactivity = useMemo(() => plotInteractivity(spec), [spec]);
  const canInteract = interactivity.mode === "full";
  const hasZoom = domains !== null;

  const onView = useCallback((view: PlotView | null) => {
    viewRef.current = view;
  }, []);
  const onBrush = useCallback((next: PlotDomains) => {
    setDomains(next);
  }, []);
  const onReset = useCallback(() => {
    setDomains(null);
    setRenderKey((key) => key + 1);
  }, []);
  const onSavePng = useCallback(async () => {
    const view = viewRef.current;
    if (!view) return;
    try {
      const url = await view.toImageURL("png", 2);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = plotPngFilename(title);
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch {
      // Silent — the reset button is still available if the export fails.
    }
  }, [title]);

  return (
    <div className="artifact-plot-detail">
      <div className="artifact-detail-toolbar">
        <button
          type="button"
          onClick={onReset}
          disabled={!canInteract || !hasZoom}
          aria-label="Reset zoom"
        >
          <RotateCcw size={12} /> Reset zoom
        </button>
        <button type="button" onClick={onSavePng} aria-label="Save as PNG">
          <Download size={12} /> Save PNG
        </button>
        <span className="artifact-plot-hint">{HINT_BY_MODE[interactivity.mode] ?? ""}</span>
      </div>
      <div
        className="artifact-plot-detail-canvas"
        data-interactive={canInteract ? "true" : undefined}
        onDoubleClick={hasZoom ? onReset : undefined}
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
