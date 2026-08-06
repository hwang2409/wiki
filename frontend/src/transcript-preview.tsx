import { useCallback, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { hasAnsi, renderAnsi } from "./ansi";
import { STREAM_CLAMP_LINES, STREAM_CLAMP_PX, useStreamHeightOverflow } from "./stream-clamp";

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
  ariaLabel?: string;
  title?: string;
  onClick: () => void;
};

function ChipButton({ active, label, ariaLabel, title, onClick }: ChipButtonProps) {
  return (
    <button
      className={`transcript-chip${active ? " is-active" : ""}`}
      type="button"
      aria-label={ariaLabel ?? label}
      title={title ?? ariaLabel ?? label}
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
  rawText?: string;
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
  variant?: "default" | "block";
};

// Anchor scroll on the preview head so expand/collapse keeps the tool's start
// row where the reader last saw it (WIKI-241). The preview lives inside the
// virtualized `.session-scroll` container; the row's top is what stays fixed.
function findScrollContainer(node: HTMLElement | null): HTMLElement | null {
  let current: HTMLElement | null = node?.parentElement ?? null;
  while (current) {
    if (current.classList.contains("session-scroll")) return current;
    current = current.parentElement;
  }
  return null;
}

export function BoundedPreview({
  text,
  rawText = text,
  label,
  previewLines = STREAM_CLAMP_LINES,
  ansi = false,
  showSummary = true,
  className,
  tone = "normal",
  renderExpandedBody,
  renderBody,
  wrapAvailable: wrapAvailableOverride,
  expandable = true,
  variant = "default",
}: BoundedPreviewProps) {
  const [expanded, setExpanded] = useState(false);
  const [wrap, setWrap] = useState(true);
  const [copied, setCopied] = useState(false);
  const bodyRef = useRef<HTMLElement | null>(null);
  const headRef = useRef<HTMLDivElement | null>(null);
  // Line clipping alone misses payloads with few newlines but long wrapped
  // lines; the shared rendered-height threshold catches those (WIKI-222 R1-01).
  const heightOverflow = useStreamHeightOverflow(bodyRef, expandable);

  const lines = useMemo(() => (text.length === 0 ? [] : text.split("\n")), [text]);
  const totalLines = lines.length;
  const compact = totalLines <= 1 && text.length <= 240;
  const shouldClip = expandable && !expanded && totalLines > previewLines;
  const heightClamped = expandable && !expanded && heightOverflow;
  const shownLines = shouldClip ? lines.slice(0, previewLines) : lines;
  const hiddenCount = totalLines - shownLines.length;
  const summary = showSummary ? summaryLabel(text) : null;
  const usesAnsi = ansi && hasAnsi(text);
  const wrapAvailable = wrapAvailableOverride
    ?? (shownLines.some((line) => line.length > 120) || totalLines > 30);

  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(rawText).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      },
      () => setCopied(false),
    );
  }, [rawText]);

  // WIKI-241: preserve the transcript scroll position across expand/collapse.
  // Capture where the preview head sits before the toggle, then, once React
  // re-lays out, adjust the session scroller by the delta so the head is at
  // the same viewport y as before. The virtualized anchor system keeps the
  // owning row anchored, but the reader's actual line-of-sight (the head, or
  // some spot inside the long body) still shifts without this correction.
  const toggleExpanded = useCallback(() => {
    const head = headRef.current;
    const container = findScrollContainer(head);
    if (!head || !container) {
      setExpanded((value) => !value);
      return;
    }
    const containerTop = container.getBoundingClientRect().top;
    const beforeHeadTop = head.getBoundingClientRect().top;
    const beforeOffset = beforeHeadTop - containerTop;
    setExpanded((value) => !value);
    // Try to correct the drift once React has laid out. React commit runs
    // synchronously before the next paint, but the virtualized transcript
    // does one measurement pass in a useLayoutEffect that can bump row
    // heights again. So: check the first frame; if it saw no drift yet,
    // check the second frame. Never apply twice — one correction is enough
    // and repeating it compounds the delta.
    const tryCorrect = (): boolean => {
      const nextContainerTop = container.getBoundingClientRect().top;
      const nextHeadTop = head.getBoundingClientRect().top;
      const nextOffset = nextHeadTop - nextContainerTop;
      const delta = nextOffset - beforeOffset;
      if (Math.abs(delta) <= 0.5) return false;
      container.scrollTop += delta;
      return true;
    };
    requestAnimationFrame(() => {
      if (tryCorrect()) return;
      requestAnimationFrame(() => { tryCorrect(); });
    });
  }, []);

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
    <button
      aria-expanded={expanded}
      className="transcript-preview-more"
      type="button"
      onClick={toggleExpanded}
    >
      +{hiddenCount} more line{hiddenCount === 1 ? "" : "s"}
    </button>
  ) : null;

  const wrapChipLabel = wrap ? "keep lines" : "wrap lines";
  const wrapChipTitle = wrap ? "Stop wrapping long lines" : "Wrap long lines to fit";
  const expandChipLabel = expanded ? "show less" : "show all";
  const expandChipTitle = expanded
    ? "Show only the preview"
    : totalLines > previewLines
      ? `Show all ${totalLines} lines`
      : "Show full output";

  return (
    <div
      className={`transcript-preview${className ? ` ${className}` : ""} is-${tone} is-${variant}${compact ? " is-compact" : ""}${expanded ? " is-expanded" : ""}`}
    >
      {variant === "default" ? (
        <div className="transcript-preview-head" ref={headRef}>
          {label ? <span className="transcript-preview-label">{label}</span> : null}
          {summary ? <span className="transcript-preview-summary">{summary}</span> : null}
          <span className="transcript-preview-actions">
            {wrapAvailable ? (
              <ChipButton
                active={!wrap}
                label={wrapChipLabel}
                ariaLabel={wrapChipLabel}
                title={wrapChipTitle}
                onClick={() => setWrap((value) => !value)}
              />
            ) : null}
            {expandable && (totalLines > previewLines || heightOverflow) ? (
              <ChipButton
                active={expanded}
                label={expandChipLabel}
                ariaLabel={expandChipLabel}
                title={expandChipTitle}
                onClick={toggleExpanded}
              />
            ) : null}
            {text.length > 0 ? (
              <ChipButton
                label={copied ? "copied" : "copy output"}
                ariaLabel="copy output"
                title="Copy the full text to the clipboard"
                onClick={copy}
              />
            ) : null}
          </span>
        </div>
      ) : null}
      {custom ? (
        <div
          className={`transcript-preview-body is-custom${wrap ? " is-wrap" : " is-nowrap"}${heightClamped ? " is-height-clamped" : ""}`}
          ref={(el) => { bodyRef.current = el; }}
          style={heightClamped ? { maxHeight: STREAM_CLAMP_PX } : undefined}
        >
          {custom}
          {moreHint}
        </div>
      ) : (
        <pre
          className={`transcript-preview-body${wrap ? " is-wrap" : " is-nowrap"}${heightClamped ? " is-height-clamped" : ""}`}
          ref={(el) => { bodyRef.current = el; }}
          style={heightClamped ? { maxHeight: STREAM_CLAMP_PX } : undefined}
        >
          {defaultBody}
          {moreHint ? <>{"\n"}{moreHint}</> : null}
        </pre>
      )}
      {variant === "block" && !shouldClip && expandable && (totalLines > previewLines || heightOverflow) ? (
        <button
          aria-expanded={expanded}
          className="transcript-preview-more"
          type="button"
          onClick={toggleExpanded}
        >
          {expandChipLabel}
        </button>
      ) : null}
    </div>
  );
}
