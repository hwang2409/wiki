import { useState } from "react";
import type { ReactNode } from "react";
import { FileQuestion, RotateCcw } from "lucide-react";

export type ArtifactPlaceholderShape =
  | "diagram"
  | "plot"
  | "code"
  | "image"
  | "table";

export function ArtifactPlaceholder({
  label,
  shape,
}: {
  label: string;
  shape: ArtifactPlaceholderShape;
}) {
  return (
    <div
      aria-busy="true"
      aria-label={label}
      className="artifact-placeholder"
      data-artifact-placeholder-shape={shape}
      role="status"
    >
      <div className="artifact-placeholder-shape">
        {shape === "diagram" ? <PlaceholderDiagram /> : null}
        {shape === "plot" ? <PlaceholderPlot /> : null}
        {shape === "code" ? <PlaceholderCode /> : null}
        {shape === "image" ? <PlaceholderImage /> : null}
        {shape === "table" ? <PlaceholderTable /> : null}
      </div>
      <span className="artifact-placeholder-label">{label}</span>
    </div>
  );
}

function PlaceholderDiagram() {
  return (
    <svg aria-hidden="true" viewBox="0 0 240 96" preserveAspectRatio="none">
      <rect x="8" y="18" width="52" height="24" rx="4" className="skeleton" />
      <rect x="94" y="18" width="52" height="24" rx="4" className="skeleton" />
      <rect x="180" y="18" width="52" height="24" rx="4" className="skeleton" />
      <rect x="52" y="60" width="128" height="24" rx="4" className="skeleton" />
      <line x1="60" y1="30" x2="94" y2="30" className="skeleton-stroke" />
      <line x1="146" y1="30" x2="180" y2="30" className="skeleton-stroke" />
      <line x1="120" y1="42" x2="120" y2="60" className="skeleton-stroke" />
    </svg>
  );
}

function PlaceholderPlot() {
  return (
    <svg aria-hidden="true" viewBox="0 0 240 96" preserveAspectRatio="none">
      <line x1="18" y1="82" x2="230" y2="82" className="skeleton-stroke" />
      <line x1="18" y1="10" x2="18" y2="82" className="skeleton-stroke" />
      {[0, 1, 2, 3, 4, 5].map((index) => (
        <rect
          key={index}
          x={30 + index * 34}
          y={30 + (index % 3) * 12}
          width="20"
          height={50 - (index % 3) * 12}
          className="skeleton"
        />
      ))}
    </svg>
  );
}

function PlaceholderCode() {
  return (
    <div className="artifact-placeholder-lines">
      {[68, 82, 54, 90, 44, 76].map((pct, index) => (
        <span
          key={index}
          className="artifact-placeholder-line skeleton"
          style={{ width: `${pct}%` }}
        />
      ))}
    </div>
  );
}

function PlaceholderImage() {
  return (
    <svg aria-hidden="true" viewBox="0 0 240 120" preserveAspectRatio="none">
      <rect x="4" y="4" width="232" height="112" rx="4" className="skeleton" />
      <circle cx="200" cy="34" r="12" className="skeleton-alt" />
      <path
        d="M12 108 L82 68 L128 92 L184 58 L228 88 L228 112 L12 112 Z"
        className="skeleton-alt"
      />
    </svg>
  );
}

function PlaceholderTable() {
  return (
    <div className="artifact-placeholder-table">
      {Array.from({ length: 4 }).map((_, row) => (
        <div key={row} className="artifact-placeholder-table-row">
          {[38, 22, 30, 20].map((pct, col) => (
            <span
              key={col}
              className="artifact-placeholder-line skeleton"
              style={{ width: `${pct}%` }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function ArtifactError({
  detail,
  onRetry,
  title,
}: {
  detail?: string | null;
  onRetry?: () => void;
  title: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="artifact-render-error artifact-error" role="alert">
      <div className="artifact-render-error-body">
        <div className="artifact-render-error-title">{title}</div>
        {detail ? (
          <details
            className="artifact-render-error-details"
            open={open}
            onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
          >
            <summary>{open ? "Hide details" : "Show details"}</summary>
            <pre>{detail}</pre>
          </details>
        ) : null}
      </div>
      {onRetry ? (
        <button
          className="artifact-render-error-retry"
          onClick={onRetry}
          type="button"
        >
          <RotateCcw aria-hidden="true" size={12} />
          Retry
        </button>
      ) : null}
    </div>
  );
}

export function ArtifactFallback({
  actions,
  detail,
  title,
}: {
  actions?: ReactNode;
  detail?: string | null;
  title: string;
}) {
  return (
    <div className="artifact-render-fallback" role="status">
      <div className="artifact-render-fallback-icon" aria-hidden="true">
        <FileQuestion size={22} />
      </div>
      <div className="artifact-render-fallback-body">
        <div className="artifact-render-fallback-title">{title}</div>
        {detail ? <div className="artifact-render-fallback-detail">{detail}</div> : null}
      </div>
      {actions ? <div className="artifact-render-fallback-actions">{actions}</div> : null}
    </div>
  );
}
