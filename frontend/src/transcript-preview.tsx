import { useCallback, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { hasAnsi, renderAnsi } from "./ansi";

const PREVIEW_LINE_LIMIT = 6;

function byteLength(text: string): number {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(text).length;
  let bytes = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) { bytes += 4; i += 1; }
    else bytes += 3;
  }
  return bytes;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function summaryLabel(text: string): string {
  const lineCount = text.length === 0 ? 0 : text.split("\n").length;
  const bytes = byteLength(text);
  const linePart = `${lineCount} line${lineCount === 1 ? "" : "s"}`;
  return `${linePart} · ${formatBytes(bytes)}`;
}

type ChipButtonProps = {
  active?: boolean;
  label: string;
  title?: string;
  onClick: () => void;
};

function ChipButton({ active, label, title, onClick }: ChipButtonProps) {
  return (
    <button
      className={`transcript-chip${active ? " is-active" : ""}`}
      type="button"
      title={title ?? label}
      onClick={(event) => {
        event.stopPropagation();
        onClick();
      }}
    >
      {label}
    </button>
  );
}

export type BoundedPreviewRenderProps = {
  text: string;
  fullText: string;
  expanded: boolean;
  clipped: boolean;
  hiddenCount: number;
  wrap: boolean;
};

type BoundedPreviewProps = {
  text: string;
  label?: string;
  language?: "bash" | "plain";
  previewLines?: number;
  ansi?: boolean;
  showSummary?: boolean;
  className?: string;
  tone?: "normal" | "error";
  renderExpandedBody?: (text: string) => ReactNode;
  renderBody?: (props: BoundedPreviewRenderProps) => ReactNode;
  wrapAvailable?: boolean;
  expandable?: boolean;
};

export function BoundedPreview({
  text,
  label,
  previewLines = PREVIEW_LINE_LIMIT,
  ansi = false,
  showSummary = true,
  className,
  tone = "normal",
  renderExpandedBody,
  renderBody,
  wrapAvailable: wrapAvailableOverride,
  expandable = true,
}: BoundedPreviewProps) {
  const [expanded, setExpanded] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);

  const lines = useMemo(() => (text.length === 0 ? [] : text.split("\n")), [text]);
  const totalLines = lines.length;
  const shouldClip = expandable && !expanded && totalLines > previewLines;
  const shownLines = shouldClip ? lines.slice(0, previewLines) : lines;
  const hiddenCount = totalLines - shownLines.length;
  const summary = showSummary ? summaryLabel(text) : null;
  const usesAnsi = ansi && hasAnsi(text);
  const wrapAvailable = wrapAvailableOverride
    ?? (shownLines.some((line) => line.length > 120) || totalLines > 30);

  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      },
      () => setCopied(false),
    );
  }, [text]);

  const bodyText = shownLines.join("\n");
  const custom = renderBody
    ? renderBody({
        text: shouldClip ? bodyText : text,
        fullText: text,
        expanded,
        clipped: shouldClip,
        hiddenCount,
        wrap,
      })
    : null;
  const defaultBody: ReactNode = renderExpandedBody && expanded
    ? renderExpandedBody(text)
    : usesAnsi
      ? renderAnsi(bodyText)
      : bodyText;

  const moreHint = shouldClip && hiddenCount > 0 ? (
    <span className="transcript-preview-more">
      <span>+{hiddenCount} more line{hiddenCount === 1 ? "" : "s"}</span>
    </span>
  ) : null;

  return (
    <div className={`transcript-preview${className ? ` ${className}` : ""} is-${tone}`}>
      <div className="transcript-preview-head">
        {label ? <span className="transcript-preview-label">{label}</span> : null}
        {summary ? <span className="transcript-preview-summary">{summary}</span> : null}
        <span className="transcript-preview-actions">
          {wrapAvailable ? (
            <ChipButton
              active={!wrap}
              label={wrap ? "nowrap" : "wrap"}
              title={wrap ? "Disable line wrap" : "Enable line wrap"}
              onClick={() => setWrap((value) => !value)}
            />
          ) : null}
          {expandable && totalLines > previewLines ? (
            <ChipButton
              active={expanded}
              label={expanded ? "collapse" : "expand"}
              title={expanded ? "Collapse preview" : `Show all ${totalLines} lines`}
              onClick={() => setExpanded((value) => !value)}
            />
          ) : null}
          {text.length > 0 ? (
            <ChipButton
              label={copied ? "copied" : "copy"}
              title="Copy full text"
              onClick={copy}
            />
          ) : null}
        </span>
      </div>
      {custom ? (
        <div className={`transcript-preview-body is-custom${wrap ? " is-wrap" : " is-nowrap"}`}>
          {custom}
          {moreHint}
        </div>
      ) : (
        <pre className={`transcript-preview-body${wrap ? " is-wrap" : " is-nowrap"}`}>
          {defaultBody}
          {moreHint ? <>{"\n"}{moreHint}</> : null}
        </pre>
      )}
    </div>
  );
}
