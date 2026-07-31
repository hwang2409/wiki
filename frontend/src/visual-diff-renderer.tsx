import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Columns2, ScanSearch } from "lucide-react";
import type { SessionArtifact, SessionEvent } from "./api";
import { ArtifactError, ArtifactPlaceholder } from "./artifact-state";
import {
  blendOpacity,
  boundedDiffDimensions,
  computePixelDiff,
  formatDiffPercent,
  visualDiffAspect,
  visualDiffSources,
} from "./visual-diff";

type LoadedImage = {
  element: HTMLImageElement;
  width: number;
  height: number;
};

type LoadState =
  | { status: "loading" }
  | { status: "ready"; before: LoadedImage; after: LoadedImage }
  | { status: "error"; message: string };

function useVisualDiffImages(sources: { before: string; after: string }): {
  state: LoadState;
  reload: () => void;
} {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    const load = (src: string) => new Promise<LoadedImage>((resolve, reject) => {
      const image = new Image();
      image.crossOrigin = "anonymous";
      image.decoding = "async";
      image.onload = () => resolve({
        element: image,
        width: image.naturalWidth,
        height: image.naturalHeight,
      });
      image.onerror = () => reject(new Error(`failed to load ${src}`));
      image.src = src;
    });
    Promise.all([load(sources.before), load(sources.after)])
      .then(([before, after]) => {
        if (cancelled) return;
        setState({ status: "ready", before, after });
      })
      .catch((error) => {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Failed to load images.",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [nonce, sources.before, sources.after]);
  return { state, reload: () => setNonce((value) => value + 1) };
}

type OverlayResult = {
  changedPixels: number;
  totalPixels: number;
  scaled: boolean;
};

function drawPixelDiffOverlay(
  overlayCanvas: HTMLCanvasElement,
  before: LoadedImage,
  after: LoadedImage,
): OverlayResult | null {
  const naturalWidth = Math.min(before.width, after.width);
  const naturalHeight = Math.min(before.height, after.height);
  if (naturalWidth === 0 || naturalHeight === 0) return null;
  // Cap the working buffers so a 40 MP pair does not allocate ~500 MB and
  // freeze the main thread. The overlay canvas is stretched by CSS
  // (object-fit: contain over the stage) so downscaling stays imperceptible.
  const bounds = boundedDiffDimensions(naturalWidth, naturalHeight);
  const width = bounds.width;
  const height = bounds.height;
  if (width === 0 || height === 0) return null;
  const context = overlayCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  overlayCanvas.width = width;
  overlayCanvas.height = height;
  const scratch = document.createElement("canvas");
  scratch.width = width;
  scratch.height = height;
  const scratchContext = scratch.getContext("2d", { willReadFrequently: true });
  if (!scratchContext) return null;
  scratchContext.drawImage(before.element, 0, 0, width, height);
  const beforeData = scratchContext.getImageData(0, 0, width, height);
  scratchContext.clearRect(0, 0, width, height);
  scratchContext.drawImage(after.element, 0, 0, width, height);
  const afterData = scratchContext.getImageData(0, 0, width, height);
  const diff = computePixelDiff(beforeData.data, afterData.data, width, height);
  context.clearRect(0, 0, width, height);
  const overlayData = context.createImageData(width, height);
  overlayData.data.set(diff.overlay);
  context.putImageData(overlayData, 0, 0);
  return {
    changedPixels: diff.changedPixels,
    totalPixels: diff.totalPixels,
    scaled: bounds.scaled,
  };
}

export type VisualDiffRendererProps = {
  artifact: SessionArtifact;
  compact?: boolean;
  event: SessionEvent;
  readOnly?: boolean;
  ticket: string;
};

export function VisualDiffRenderer({
  artifact,
  compact = false,
  event,
  readOnly = false,
  ticket,
}: VisualDiffRendererProps) {
  const sources = useMemo(() => visualDiffSources(ticket, event), [ticket, event]);
  const { state, reload } = useVisualDiffImages(sources);
  const [slider, setSlider] = useState(compact ? 0.5 : 0.5);
  const [pixelDiff, setPixelDiff] = useState(false);
  const overlayRef = useRef<HTMLCanvasElement | null>(null);
  const [diffStats, setDiffStats] = useState<{
    changedPixels: number;
    totalPixels: number;
    scaled: boolean;
  } | null>(null);
  const [overlayError, setOverlayError] = useState<string | null>(null);
  const aspect = visualDiffAspect(artifact);
  // useId gives every mounted copy its own value, so an inline + inspector +
  // panel triple of the same artifact does not collide on DOM ids or bind a
  // label to the wrong slider. Must live above the conditional error return
  // so the hook count is stable across load / ready / error transitions.
  const reactId = useId();
  const sliderId = `visual-diff-slider-${reactId}`;

  useEffect(() => {
    if (!pixelDiff || state.status !== "ready") return;
    const canvas = overlayRef.current;
    if (!canvas) return;
    try {
      const stats = drawPixelDiffOverlay(canvas, state.before, state.after);
      if (stats) {
        setDiffStats(stats);
        setOverlayError(null);
      } else {
        setOverlayError("Could not sample images for pixel diff.");
      }
    } catch (error) {
      setOverlayError(
        error instanceof Error ? error.message : "Pixel diff failed.",
      );
    }
  }, [pixelDiff, state]);

  useEffect(() => {
    if (!pixelDiff) {
      setDiffStats(null);
      setOverlayError(null);
      const canvas = overlayRef.current;
      if (canvas) {
        const context = canvas.getContext("2d");
        context?.clearRect(0, 0, canvas.width, canvas.height);
      }
    }
  }, [pixelDiff]);

  if (state.status === "error") {
    return (
      <ArtifactError
        detail={state.message}
        onRetry={reload}
        title="Visual diff couldn’t load."
      />
    );
  }

  const opacity = blendOpacity(slider);
  const aspectStyle = aspect ? { aspectRatio: `${aspect}` } : undefined;
  const stageContent = state.status === "ready" ? (
    <>
      <img
        alt={event.title || event.caption || "Before"}
        className="visual-diff-image is-before"
        decoding="async"
        src={sources.before}
      />
      <img
        alt="After"
        aria-hidden="true"
        className="visual-diff-image is-after"
        decoding="async"
        src={sources.after}
        style={{ opacity }}
      />
      {pixelDiff ? (
        <canvas
          aria-hidden="true"
          className="visual-diff-overlay"
          ref={overlayRef}
        />
      ) : null}
    </>
  ) : (
    <ArtifactPlaceholder label="Loading images…" shape="image" />
  );

  const diffPercent = diffStats
    ? formatDiffPercent(diffStats.changedPixels, diffStats.totalPixels)
    : null;

  return (
    <div
      className={`visual-diff${compact ? " is-compact" : ""}${pixelDiff ? " has-diff-overlay" : ""}`}
      data-state={state.status}
    >
      <div className="visual-diff-stage" style={aspectStyle}>
        {stageContent}
      </div>
      {readOnly ? null : <div className="visual-diff-controls">
        <label className="visual-diff-slider" htmlFor={sliderId}>
          <span className="visual-diff-slider-label">
            <Columns2 aria-hidden="true" size={12} /> Blend
          </span>
          <span className="visual-diff-slider-terminals" aria-hidden="true">
            <span>Before</span>
            <span>After</span>
          </span>
          <input
            className="visual-diff-slider-input"
            disabled={state.status !== "ready"}
            id={sliderId}
            max={1}
            min={0}
            onChange={(changeEvent) => setSlider(Number(changeEvent.target.value))}
            step={0.01}
            type="range"
            value={slider}
          />
          <span className="visual-diff-slider-readout tabular-nums">
            {Math.round(opacity * 100)}%
          </span>
        </label>
        <button
          aria-pressed={pixelDiff}
          className={`visual-diff-toggle${pixelDiff ? " is-on" : ""}`}
          disabled={state.status !== "ready"}
          onClick={() => setPixelDiff((value) => !value)}
          type="button"
        >
          <ScanSearch aria-hidden="true" size={12} />
          <span>Pixel diff</span>
          {diffPercent ? (
            <span className="visual-diff-toggle-readout tabular-nums">{diffPercent}</span>
          ) : null}
        </button>
      </div>}
      {overlayError ? (
        <div className="visual-diff-overlay-error" role="status">{overlayError}</div>
      ) : null}
    </div>
  );
}
