import { createPortal } from "react-dom";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  DragEvent as ReactDragEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
} from "react";
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  Maximize2,
  Minus,
  Plus,
  RotateCcw,
  X,
} from "lucide-react";

export type LightboxItem = {
  src: string;
  alt: string;
  caption?: string | null;
  downloadName?: string | null;
};

export type ArtifactLightboxProps = {
  items: LightboxItem[];
  index: number;
  onIndexChange: (next: number) => void;
  onClose: () => void;
};

type Transform = { zoom: number; panX: number; panY: number };

const MIN_ZOOM = 0.2;
const MAX_ZOOM = 8;
const RESET_TRANSFORM: Transform = { zoom: 1, panX: 0, panY: 0 };

function clampZoom(value: number) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

function guessDownloadName(item: LightboxItem, index: number) {
  if (item.downloadName) return item.downloadName;
  try {
    const parsed = new URL(item.src, window.location.origin);
    const tail = parsed.pathname.split("/").pop();
    if (tail) return tail;
  } catch {
    // data:/blob: URLs fall through
  }
  return `image-${index + 1}`;
}

async function copySourceToClipboard(item: LightboxItem): Promise<boolean> {
  if (!navigator.clipboard) return false;
  try {
    if (item.src.startsWith("data:")) {
      const [meta, data] = item.src.slice(5).split(",");
      const mime = meta.split(";")[0] || "image/png";
      if (typeof ClipboardItem !== "undefined") {
        const binary = atob(data);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        const blob = new Blob([bytes], { type: mime });
        await navigator.clipboard.write([new ClipboardItem({ [mime]: blob })]);
        return true;
      }
    } else if (typeof ClipboardItem !== "undefined") {
      const response = await fetch(item.src);
      const blob = await response.blob();
      await navigator.clipboard.write([new ClipboardItem({ [blob.type || "image/png"]: blob })]);
      return true;
    }
  } catch {
    // fall through to URL copy
  }
  try {
    await navigator.clipboard.writeText(item.src);
    return true;
  } catch {
    return false;
  }
}

export function ArtifactLightbox({
  index,
  items,
  onClose,
  onIndexChange,
}: ArtifactLightboxProps) {
  const [transform, setTransform] = useState<Transform>(RESET_TRANSFORM);
  const [copied, setCopied] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const lastDistance = useRef<number | null>(null);
  const viewportRef = useRef<HTMLDivElement | null>(null);

  const active = items[index];
  const total = items.length;
  const canPaginate = total > 1;

  const reset = useCallback(() => setTransform(RESET_TRANSFORM), []);

  useEffect(() => {
    reset();
    setLoaded(false);
  }, [index, active?.src, reset]);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
      previouslyFocused.current?.focus?.();
    };
  }, []);

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.0015);
      setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom * factor) }));
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, []);

  const handleKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      switch (event.key) {
        case "Escape":
          event.preventDefault();
          onClose();
          return;
        case "ArrowLeft":
          if (canPaginate) {
            event.preventDefault();
            onIndexChange((index - 1 + total) % total);
          }
          return;
        case "ArrowRight":
          if (canPaginate) {
            event.preventDefault();
            onIndexChange((index + 1) % total);
          }
          return;
        case "+":
        case "=":
          event.preventDefault();
          setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom * 1.25) }));
          return;
        case "-":
        case "_":
          event.preventDefault();
          setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom / 1.25) }));
          return;
        case "0":
          event.preventDefault();
          reset();
          return;
        default:
          return;
      }
    },
    [canPaginate, index, onClose, onIndexChange, reset, total],
  );

  const onPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 2) {
      const [first, second] = [...pointers.current.values()];
      lastDistance.current = Math.hypot(second.x - first.x, second.y - first.y);
    }
  };

  const onPointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 1) {
      setTransform((prev) => ({
        ...prev,
        panX: prev.panX + event.clientX - previous.x,
        panY: prev.panY + event.clientY - previous.y,
      }));
      return;
    }
    const [first, second] = [...pointers.current.values()];
    const distance = Math.hypot(second.x - first.x, second.y - first.y);
    if (lastDistance.current && lastDistance.current > 0) {
      const factor = distance / lastDistance.current;
      setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom * factor) }));
    }
    lastDistance.current = distance;
  };

  const releasePointer = (event: ReactPointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) lastDistance.current = null;
  };

  const zoomIn = () => setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom * 1.25) }));
  const zoomOut = () => setTransform((prev) => ({ ...prev, zoom: clampZoom(prev.zoom / 1.25) }));

  const copy = async () => {
    if (!active) return;
    const ok = await copySourceToClipboard(active);
    if (ok) {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    }
  };

  const download = () => {
    if (!active) return;
    const anchor = document.createElement("a");
    anchor.href = active.src;
    anchor.download = guessDownloadName(active, index);
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
  };

  const onDragStart = (event: ReactDragEvent<HTMLImageElement>) => {
    if (!active) return;
    const name = guessDownloadName(active, index);
    const mime = active.src.startsWith("data:")
      ? active.src.slice(5).split(";")[0] || "image/png"
      : "image/png";
    event.dataTransfer.effectAllowed = "copy";
    event.dataTransfer.setData("text/uri-list", active.src);
    event.dataTransfer.setData("text/plain", active.src);
    if (!active.src.startsWith("data:")) {
      event.dataTransfer.setData("DownloadURL", `${mime}:${name}:${new URL(active.src, window.location.origin).toString()}`);
    }
  };

  const overlay = useMemo(() => {
    if (!active) return null;
    return (
      <div
        aria-label={active.alt}
        aria-modal="true"
        className="artifact-lightbox"
        onClick={(event) => {
          if (event.target === event.currentTarget) onClose();
        }}
        onKeyDown={handleKeyDown}
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="artifact-lightbox-topbar">
          <div className="artifact-lightbox-title" data-tabular>
            <span className="artifact-lightbox-title-caption">{active.caption ?? active.alt}</span>
            {canPaginate ? (
              <span className="artifact-lightbox-title-counter tabular-nums">
                {index + 1} <span aria-hidden="true">/</span> {total}
              </span>
            ) : null}
          </div>
          <div className="artifact-lightbox-actions">
            <button
              className="artifact-lightbox-action"
              onClick={zoomOut}
              title="Zoom out (−)"
              type="button"
            >
              <Minus size={14} />
            </button>
            <span className="artifact-lightbox-zoom tabular-nums">
              {Math.round(transform.zoom * 100)}%
            </span>
            <button
              className="artifact-lightbox-action"
              onClick={zoomIn}
              title="Zoom in (+)"
              type="button"
            >
              <Plus size={14} />
            </button>
            <button
              className="artifact-lightbox-action"
              onClick={reset}
              title="Reset view (0)"
              type="button"
            >
              <RotateCcw size={14} />
            </button>
            <span className="artifact-lightbox-divider" aria-hidden="true" />
            <button
              className="artifact-lightbox-action"
              onClick={() => void copy()}
              title={copied ? "Copied" : "Copy image"}
              type="button"
            >
              <Copy size={14} />
              <span className="artifact-lightbox-action-label">
                {copied ? "Copied" : "Copy"}
              </span>
            </button>
            <button
              className="artifact-lightbox-action"
              onClick={download}
              title="Download image"
              type="button"
            >
              <Download size={14} />
              <span className="artifact-lightbox-action-label">Save</span>
            </button>
            <button
              className="artifact-lightbox-action is-close"
              onClick={onClose}
              title="Close (Esc)"
              type="button"
            >
              <X size={16} />
            </button>
          </div>
        </div>
        <div
          className="artifact-lightbox-viewport"
          onPointerCancel={releasePointer}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={releasePointer}
          ref={viewportRef}
        >
          {!loaded ? (
            <div className="artifact-lightbox-loading" aria-hidden="true">
              <div className="artifact-lightbox-loading-shimmer" />
            </div>
          ) : null}
          <img
            alt={active.alt}
            className={`artifact-lightbox-image${loaded ? " is-loaded" : ""}`}
            draggable
            onDragStart={onDragStart}
            onLoad={() => setLoaded(true)}
            src={active.src}
            style={{
              transform: `translate3d(${transform.panX}px, ${transform.panY}px, 0) scale(${transform.zoom})`,
              cursor: transform.zoom > 1 ? "grab" : "zoom-in",
            }}
          />
        </div>
        {canPaginate ? (
          <>
            <button
              aria-label="Previous image"
              className="artifact-lightbox-nav is-prev"
              onClick={() => onIndexChange((index - 1 + total) % total)}
              type="button"
            >
              <ChevronLeft size={22} />
            </button>
            <button
              aria-label="Next image"
              className="artifact-lightbox-nav is-next"
              onClick={() => onIndexChange((index + 1) % total)}
              type="button"
            >
              <ChevronRight size={22} />
            </button>
          </>
        ) : null}
        <div className="artifact-lightbox-hint" aria-hidden="true">
          <Maximize2 size={11} /> scroll to zoom · drag to pan {canPaginate ? "· ← → to navigate" : ""}
        </div>
      </div>
    );
  }, [
    active,
    canPaginate,
    copied,
    handleKeyDown,
    index,
    loaded,
    onClose,
    onIndexChange,
    reset,
    total,
    transform.panX,
    transform.panY,
    transform.zoom,
  ]);

  if (!overlay || typeof document === "undefined") return null;
  return createPortal(overlay, document.body);
}
