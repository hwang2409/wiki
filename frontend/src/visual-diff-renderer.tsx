import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Columns2, ScanSearch } from "lucide-react";
import type { SessionArtifact, SessionEvent } from "./api";
import { ArtifactError, ArtifactPlaceholder } from "./artifact-state";
import {
  boundedDiffDimensions,
  computePixelDiff,
  formatDiffPercent,
  NATIVE_DIFF_TILE,
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

// Async yield helper — parks work on the macrotask queue so React can paint
// and any pending cancel signal can reach us before the next tile runs. A
// setTimeout(0) is the widely-supported way to yield on the web; scheduler.
// yield() is nicer but not yet universal.
function yieldToEventLoop(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

async function drawPixelDiffOverlay(
  overlayCanvas: HTMLCanvasElement,
  before: LoadedImage,
  after: LoadedImage,
  signal: AbortSignal,
): Promise<OverlayResult | null> {
  const nativeWidth = Math.min(before.width, after.width);
  const nativeHeight = Math.min(before.height, after.height);
  if (nativeWidth === 0 || nativeHeight === 0) return null;
  // Two-tier design (WIKI-193 review round 5+6): compare in
  // native-resolution tiles so a localized one-pixel regression on a 40 MP
  // screenshot is not averaged away by a bilinear downsample. The visible
  // overlay stays at a bounded resolution (so a 40 MP canvas does not
  // allocate ~500 MB), and every changed native pixel is projected onto
  // its corresponding bounded pixel so the highlight remains visible even
  // for pinpoint differences. Between tiles the work yields to the event
  // loop and checks its abort signal — a full 40 MP pass ran ~0.9 s of
  // pure JS synchronously before, freezing the webview; now each tile is
  // ~2 ms, interleaved with paints, and a toggle-off cancels the rest.
  const bounds = boundedDiffDimensions(nativeWidth, nativeHeight);
  if (bounds.width === 0 || bounds.height === 0) return null;
  const context = overlayCanvas.getContext("2d", { willReadFrequently: true });
  if (!context) return null;
  overlayCanvas.width = bounds.width;
  overlayCanvas.height = bounds.height;
  const scratch = document.createElement("canvas");
  const scratchContext = scratch.getContext("2d", { willReadFrequently: true });
  if (!scratchContext) return null;
  const boundedOverlay = new Uint8ClampedArray(bounds.width * bounds.height * 4);
  const scaleX = bounds.width / nativeWidth;
  const scaleY = bounds.height / nativeHeight;
  let changedTotal = 0;

  for (let tileY = 0; tileY < nativeHeight; tileY += NATIVE_DIFF_TILE) {
    for (let tileX = 0; tileX < nativeWidth; tileX += NATIVE_DIFF_TILE) {
      if (signal.aborted) return null;
      const tileW = Math.min(NATIVE_DIFF_TILE, nativeWidth - tileX);
      const tileH = Math.min(NATIVE_DIFF_TILE, nativeHeight - tileY);
      scratch.width = tileW;
      scratch.height = tileH;
      scratchContext.imageSmoothingEnabled = false;
      scratchContext.drawImage(
        before.element,
        tileX, tileY, tileW, tileH,
        0, 0, tileW, tileH,
      );
      const beforeData = scratchContext.getImageData(0, 0, tileW, tileH);
      scratchContext.clearRect(0, 0, tileW, tileH);
      scratchContext.drawImage(
        after.element,
        tileX, tileY, tileW, tileH,
        0, 0, tileW, tileH,
      );
      const afterData = scratchContext.getImageData(0, 0, tileW, tileH);
      const diff = computePixelDiff(beforeData.data, afterData.data, tileW, tileH);
      changedTotal += diff.changedPixels;

      // Project every changed native pixel onto its bounded overlay pixel.
      // The tile overlay already carries the highlight color + alpha, so we
      // simply mark the projected pixel — a native single-pixel change lands
      // as a single bounded pixel of highlight, visible even at 40x scale.
      for (let i = 0; i < diff.overlay.length; i += 4) {
        const alpha = diff.overlay[i + 3];
        if (alpha === 0) continue;
        const pixelIndex = i >> 2;
        const localX = pixelIndex % tileW;
        const localY = (pixelIndex - localX) / tileW;
        const nativeX = tileX + localX;
        const nativeY = tileY + localY;
        const boundedX = Math.min(bounds.width - 1, Math.floor(nativeX * scaleX));
        const boundedY = Math.min(bounds.height - 1, Math.floor(nativeY * scaleY));
        const boundedIndex = (boundedY * bounds.width + boundedX) * 4;
        boundedOverlay[boundedIndex] = diff.overlay[i];
        boundedOverlay[boundedIndex + 1] = diff.overlay[i + 1];
        boundedOverlay[boundedIndex + 2] = diff.overlay[i + 2];
        boundedOverlay[boundedIndex + 3] = alpha;
      }
      await yieldToEventLoop();
    }
  }
  if (signal.aborted) return null;

  context.clearRect(0, 0, bounds.width, bounds.height);
  const overlayData = context.createImageData(bounds.width, bounds.height);
  overlayData.data.set(boundedOverlay);
  context.putImageData(overlayData, 0, 0);
  return {
    changedPixels: changedTotal,
    totalPixels: nativeWidth * nativeHeight,
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
  const compositeRef = useRef<HTMLCanvasElement | null>(null);
  const aspect = visualDiffAspect(artifact);
  // useId gives every mounted copy its own value, so an inline + inspector +
  // panel triple of the same artifact does not collide on DOM ids or bind a
  // label to the wrong slider. Must live above the conditional error return
  // so the hook count is stable across load / ready / error transitions.
  const reactId = useId();
  const sliderId = `visual-diff-slider-${reactId}`;

  // Composite blend (WIKI-193 review round 8): the old two-<img> stack faded
  // the after image's opacity over a fully visible before, so transparent
  // after-pixels always showed the before beneath — the "After" endpoint
  // was really "before under after with holes", and alpha regressions
  // hid entirely. Render both variants onto a bounded canvas per slider
  // change; endpoints skip the wrong side entirely, intermediate frames
  // use additive premultiplied-alpha blend so the linear mix is exact.
  useEffect(() => {
    if (state.status !== "ready") return;
    const canvas = compositeRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;
    const nativeWidth = Math.min(state.before.width, state.after.width);
    const nativeHeight = Math.min(state.before.height, state.after.height);
    if (nativeWidth === 0 || nativeHeight === 0) return;
    const bounds = boundedDiffDimensions(nativeWidth, nativeHeight);
    if (bounds.width === 0 || bounds.height === 0) return;
    if (canvas.width !== bounds.width) canvas.width = bounds.width;
    if (canvas.height !== bounds.height) canvas.height = bounds.height;
    context.clearRect(0, 0, bounds.width, bounds.height);
    context.imageSmoothingEnabled = true;
    context.globalCompositeOperation = "source-over";
    // Endpoint fidelity: exact-before at 0, exact-after at 1. Skipping the
    // other side avoids the composite artifacts that show up at the extremes
    // (e.g. a fully-transparent after would blend with an already-drawn
    // before if we did not skip).
    if (slider <= 0) {
      context.globalAlpha = 1;
      context.drawImage(state.before.element, 0, 0, bounds.width, bounds.height);
    } else if (slider >= 1) {
      context.globalAlpha = 1;
      context.drawImage(state.after.element, 0, 0, bounds.width, bounds.height);
    } else {
      // Premultiplied-alpha linear blend: draw before scaled by (1-s), then
      // add after scaled by s using "lighter" (additive) mode. The result is
      // dst_color = before * (1 - s) + after * s AND
      // dst_alpha = beforeAlpha * (1 - s) + afterAlpha * s — a true linear
      // interpolation whose alpha correctly falls to zero where both sides
      // are transparent.
      context.globalAlpha = 1 - slider;
      context.drawImage(state.before.element, 0, 0, bounds.width, bounds.height);
      context.globalCompositeOperation = "lighter";
      context.globalAlpha = slider;
      context.drawImage(state.after.element, 0, 0, bounds.width, bounds.height);
    }
  }, [state, slider]);

  useEffect(() => {
    if (!pixelDiff || state.status !== "ready") return;
    const canvas = overlayRef.current;
    if (!canvas) return;
    const controller = new AbortController();
    (async () => {
      try {
        const stats = await drawPixelDiffOverlay(canvas, state.before, state.after, controller.signal);
        if (controller.signal.aborted) return;
        if (stats) {
          setDiffStats(stats);
          setOverlayError(null);
        } else {
          setOverlayError("Could not sample images for pixel diff.");
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setOverlayError(
          error instanceof Error ? error.message : "Pixel diff failed.",
        );
      }
    })();
    // Cleanup runs on toggle-off, unmount, or when `state` changes (e.g. a
    // reload) — any of those should stop stale tile work from landing on
    // the canvas or reporting numbers for the old image pair.
    return () => controller.abort();
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

  const aspectStyle = aspect ? { aspectRatio: `${aspect}` } : undefined;
  const compositeLabel = event.title || event.caption || "Visual diff";
  const stageContent = (
    <>
      <canvas
        aria-label={compositeLabel}
        className="visual-diff-composite"
        data-slider={slider}
        data-testid="visual-diff-composite"
        ref={compositeRef}
        role="img"
      />
      {state.status !== "ready" ? (
        <ArtifactPlaceholder label="Loading images…" shape="image" />
      ) : null}
      {pixelDiff ? (
        <canvas
          aria-hidden="true"
          className="visual-diff-overlay"
          ref={overlayRef}
        />
      ) : null}
    </>
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
            {Math.round(slider * 100)}%
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
