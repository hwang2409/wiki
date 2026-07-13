import { useEffect, useRef } from "react";
import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { Maximize2, RotateCcw } from "lucide-react";
import type { ArtifactViewState } from "../transcript-store";

const MIN_ZOOM = 0.1;
const MAX_ZOOM = 8;

function clampZoom(value: number) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

export function PanZoomCanvas({
  children,
  label,
  onChange,
  state,
}: {
  children: ReactNode;
  label: string;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
}) {
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const lastDistance = useRef<number | null>(null);
  const zoom = state.zoom ?? 1;
  const panX = state.panX ?? 0;
  const panY = state.panY ?? 0;

  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.0015);
      onChange({ ...state, zoom: clampZoom((state.zoom ?? 1) * factor) });
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => viewport.removeEventListener("wheel", onWheel);
  }, [onChange, state]);

  function update(next: Partial<ArtifactViewState>) {
    onChange({ ...state, ...next });
  }

  function reset() {
    update({ zoom: 1, panX: 0, panY: 0 });
  }

  function fit() {
    const viewport = viewportRef.current;
    const content = viewport?.querySelector("img, svg") as HTMLImageElement | SVGSVGElement | null;
    if (!viewport || !content) {
      reset();
      return;
    }
    const viewportBox = viewport.getBoundingClientRect();
    let width = content.getBoundingClientRect().width / zoom;
    let height = content.getBoundingClientRect().height / zoom;
    if (content instanceof HTMLImageElement && content.naturalWidth > 0) {
      width = content.naturalWidth;
      height = content.naturalHeight;
    } else if (content instanceof SVGSVGElement && content.viewBox.baseVal.width > 0) {
      width = content.viewBox.baseVal.width;
      height = content.viewBox.baseVal.height;
    }
    const nextZoom = clampZoom(Math.min((viewportBox.width - 48) / width, (viewportBox.height - 48) / height, 1));
    update({ zoom: nextZoom, panX: 0, panY: 0 });
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 2) {
      const [first, second] = [...pointers.current.values()];
      lastDistance.current = Math.hypot(second.x - first.x, second.y - first.y);
    }
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size === 1) {
      update({ panX: panX + event.clientX - previous.x, panY: panY + event.clientY - previous.y });
      return;
    }
    const [first, second] = [...pointers.current.values()];
    const distance = Math.hypot(second.x - first.x, second.y - first.y);
    if (lastDistance.current && lastDistance.current > 0) {
      update({ zoom: clampZoom(zoom * distance / lastDistance.current) });
    }
    lastDistance.current = distance;
  }

  function releasePointer(event: ReactPointerEvent<HTMLDivElement>) {
    pointers.current.delete(event.pointerId);
    lastDistance.current = null;
  }

  return (
    <div className="artifact-panzoom-detail">
      <div className="artifact-detail-toolbar">
        <button type="button" onClick={fit}><Maximize2 size={12} /> Fit</button>
        <button type="button" onClick={reset}>1:1</button>
        <button type="button" onClick={reset}>Actual size</button>
        <button data-panel-reset-zoom="true" type="button" onClick={reset}><RotateCcw size={12} /> Reset</button>
        <span className="artifact-zoom-value tabular-nums">{Math.round(zoom * 100)}%</span>
      </div>
      <div
        aria-label={label}
        className="artifact-panzoom-viewport"
        ref={viewportRef}
        tabIndex={0}
        onPointerCancel={releasePointer}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={releasePointer}
      >
        <div
          className="artifact-panzoom-content"
          style={{ transform: `translate(${panX}px, ${panY}px) scale(${zoom})` }}
        >
          {children}
        </div>
      </div>
    </div>
  );
}
