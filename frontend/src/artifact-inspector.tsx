import { createPortal } from "react-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  FileJson,
  Image as ImageIcon,
  X,
} from "lucide-react";
import type { SessionEvent } from "./api";
import { KIND_ICONS } from "./artifact-block";
import { CodeArtifactDetail } from "./artifact-detail/code";
import { DiffArtifactDetail } from "./artifact-detail/diff";
import { FileListArtifactDetail } from "./artifact-detail/file-list";
import { ImageArtifactDetail } from "./artifact-detail/image";
import { JsonArtifactDetail } from "./artifact-detail/json";
import { MermaidArtifactDetail } from "./artifact-detail/mermaid";
import { PdfArtifactDetail } from "./artifact-detail/pdf";
import { PlotArtifactDetail } from "./artifact-detail/plot";
import { SvgArtifactDetail } from "./artifact-detail/svg";
import { TableArtifactDetail } from "./artifact-detail/table";
import { classifyArtifact, humanizeArtifactKind } from "./artifact-kind";
import { downloadArtifact, imageBase64, textPayload } from "./artifact-payload";
import { artifactUrl } from "./artifact-renderers";
import { ArtifactFallback } from "./artifact-state";
import type { ArtifactViewState } from "./transcript-store";

export type ArtifactInspectorProps = {
  events: SessionEvent[];
  index: number;
  onClose: () => void;
  onIndexChange: (next: number) => void;
  ticket: string;
};

const FOCUSABLE_SELECTOR =
  'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function focusableWithin(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (element) => !element.hasAttribute("aria-hidden") && element.offsetParent !== null,
  );
}

export function isTextEntryTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

export function inspectorTitle(event: SessionEvent): string {
  return event.title
    || event.artifact?.filename
    || humanizeArtifactKind(event.artifact?.kind);
}

/**
 * Resolve which artifact cmd+enter should open: the focused artifact wins,
 * then the hovered one, then the artifact panel's focused tab, then the most
 * recent artifact in the stream.
 */
export function resolveInspectTarget(
  scope: HTMLElement | Document | null,
  events: SessionEvent[],
  panelFocusedTab: string | null,
): number | null {
  if (events.length === 0) return null;
  const indexOf = (artifactId: string | null | undefined): number =>
    artifactId ? events.findIndex((event) => event.artifact_id === artifactId) : -1;
  const focusedHost = document.activeElement?.closest?.("[data-artifact-id]");
  const focusedIndex = indexOf(focusedHost?.getAttribute("data-artifact-id"));
  if (focusedIndex >= 0) return focusedIndex;
  if (scope) {
    const hovered = Array.from(scope.querySelectorAll<HTMLElement>("[data-artifact-id]"))
      .filter((element) => element.matches(":hover"))
      .at(-1);
    const hoveredIndex = indexOf(hovered?.getAttribute("data-artifact-id"));
    if (hoveredIndex >= 0) return hoveredIndex;
  }
  const panelIndex = indexOf(panelFocusedTab);
  if (panelIndex >= 0) return panelIndex;
  return events.length - 1;
}

async function copyImagePayload(ticket: string, event: SessionEvent): Promise<boolean> {
  const artifact = event.artifact!;
  const source = artifact.data_base64
    ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
    : artifactUrl(ticket, event);
  try {
    if (typeof ClipboardItem !== "undefined" && navigator.clipboard?.write) {
      const response = await fetch(source);
      const blob = await response.blob();
      await navigator.clipboard.write([
        new ClipboardItem({ [blob.type || "image/png"]: blob }),
      ]);
      return true;
    }
  } catch {
    // fall through to text copy
  }
  try {
    await navigator.clipboard.writeText(source);
    return true;
  } catch {
    return false;
  }
}

export function ArtifactInspector({
  events,
  index,
  onClose,
  onIndexChange,
  ticket,
}: ArtifactInspectorProps) {
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);
  const copyTimer = useRef<number | null>(null);
  const [copied, setCopied] = useState<"source" | "image" | null>(null);
  const [viewStates, setViewStates] = useState<Record<string, ArtifactViewState>>({});

  const active = events[index] as SessionEvent | undefined;
  const artifact = active?.artifact;
  const artifactId = active?.artifact_id ?? "";
  const total = events.length;
  const canPaginate = total > 1;

  useEffect(() => {
    setCopied(null);
    if (copyTimer.current !== null) {
      window.clearTimeout(copyTimer.current);
      copyTimer.current = null;
    }
  }, [artifactId]);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    // Inert every direct child of <body> except our portal parent so screen
    // readers and tab focus can't wander into the underlying page.
    const inertRoots: HTMLElement[] = [];
    for (const child of Array.from(document.body.children)) {
      if (!(child instanceof HTMLElement)) continue;
      if (child.contains(dialogRef.current)) continue;
      if (child.hasAttribute("inert")) continue;
      child.setAttribute("inert", "");
      child.setAttribute("aria-hidden", "true");
      inertRoots.push(child);
    }
    return () => {
      document.body.style.overflow = previousOverflow;
      for (const root of inertRoots) {
        root.removeAttribute("inert");
        root.removeAttribute("aria-hidden");
      }
      previouslyFocused.current?.focus?.();
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    };
  }, []);

  const step = useCallback(
    (offset: number) => {
      if (total < 2) return;
      onIndexChange((index + offset + total) % total);
    },
    [index, onIndexChange, total],
  );

  const handleKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLDivElement>) => {
      if (event.defaultPrevented) return;
      if (event.key === "Tab" && dialogRef.current) {
        const focusables = focusableWithin(dialogRef.current);
        if (focusables.length === 0) {
          event.preventDefault();
          dialogRef.current.focus();
          return;
        }
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        const current = document.activeElement as HTMLElement | null;
        if (event.shiftKey && (current === first || current === dialogRef.current)) {
          event.preventDefault();
          last.focus();
          return;
        }
        if (!event.shiftKey && current === last) {
          event.preventDefault();
          first.focus();
        }
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (isTextEntryTarget(event.target)) return;
      if (event.key === "ArrowLeft" && canPaginate) {
        event.preventDefault();
        step(-1);
        return;
      }
      if (event.key === "ArrowRight" && canPaginate) {
        event.preventDefault();
        step(1);
      }
    },
    [canPaginate, onClose, step],
  );

  const showCopied = useCallback((which: "source" | "image") => {
    setCopied(which);
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => {
      setCopied(null);
      copyTimer.current = null;
    }, 1400);
  }, []);

  const copySource = useCallback(async () => {
    if (!active || !artifact) return;
    const needsBinaryFetch =
      (artifact.kind === "image" && !artifact.data_base64) || artifact.kind === "pdf";
    const value = needsBinaryFetch
      ? await imageBase64(artifactUrl(ticket, active))
      : textPayload(artifact);
    await navigator.clipboard.writeText(value);
    showCopied("source");
  }, [active, artifact, showCopied, ticket]);

  const copyImage = useCallback(async () => {
    if (!active) return;
    if (await copyImagePayload(ticket, active)) showCopied("image");
  }, [active, showCopied, ticket]);

  const viewState = viewStates[artifactId] ?? {};
  const onViewChange = useCallback(
    (next: ArtifactViewState) => {
      if (!artifactId) return;
      setViewStates((current) => ({ ...current, [artifactId]: next }));
    },
    [artifactId],
  );

  if (typeof document === "undefined") return null;

  const kind = artifact ? classifyArtifact(artifact) : null;
  const Icon = (artifact && KIND_ICONS[artifact.kind]) || FileJson;
  const detail = active && artifact && kind ? (() => {
    switch (kind) {
      case "table": return <TableArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "image": return <ImageArtifactDetail artifact={artifact} event={active} onChange={onViewChange} state={viewState} ticket={ticket} />;
      case "mermaid": return <MermaidArtifactDetail onChange={onViewChange} source={artifact.source ?? ""} state={viewState} />;
      case "svg": return <SvgArtifactDetail onChange={onViewChange} source={artifact.source ?? ""} state={viewState} />;
      case "plot": return <PlotArtifactDetail spec={artifact.spec_vega_lite ?? {}} />;
      case "diff": return <DiffArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "file-list": return <FileListArtifactDetail artifact={artifact} />;
      case "json": return <JsonArtifactDetail artifact={artifact} />;
      case "code": return <CodeArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "pdf": return <PdfArtifactDetail artifact={artifact} event={active} onChange={onViewChange} state={viewState} ticket={ticket} />;
    }
  })() : null;

  const title = active ? inspectorTitle(active) : "Artifact";
  const caption = active?.caption && active.caption !== title ? active.caption : null;

  const overlay = (
    <div
      aria-label={title}
      aria-modal="true"
      className="artifact-inspector"
      data-artifact-inspector-kind={kind ?? undefined}
      onKeyDown={handleKeyDown}
      ref={dialogRef}
      role="dialog"
      tabIndex={-1}
    >
      <header className="artifact-inspector-topbar">
        <div className="artifact-inspector-heading">
          <Icon aria-hidden="true" size={14} />
          <span className="artifact-inspector-title" title={title}>{title}</span>
          {caption ? (
            <span className="artifact-inspector-caption" title={caption}>{caption}</span>
          ) : null}
          <span className="artifact-inspector-kind">{humanizeArtifactKind(artifact?.kind)}</span>
        </div>
        <div className="artifact-inspector-actions">
          {canPaginate ? (
            <>
              <button
                aria-label="Previous artifact"
                className="artifact-inspector-action is-icon"
                onClick={() => step(-1)}
                title="Previous artifact (←)"
                type="button"
              >
                <ChevronLeft size={14} />
              </button>
              <span className="artifact-inspector-counter tabular-nums">
                {index + 1} <span aria-hidden="true">/</span> {total}
              </span>
              <button
                aria-label="Next artifact"
                className="artifact-inspector-action is-icon"
                onClick={() => step(1)}
                title="Next artifact (→)"
                type="button"
              >
                <ChevronRight size={14} />
              </button>
              <span className="artifact-inspector-divider" aria-hidden="true" />
            </>
          ) : null}
          <button
            className="artifact-inspector-action"
            onClick={() => void copySource()}
            title="Copy raw payload"
            type="button"
          >
            <Copy size={13} />
            <span className="artifact-inspector-action-label">
              {copied === "source" ? "Copied" : "Copy source"}
            </span>
          </button>
          {kind === "image" ? (
            <button
              className="artifact-inspector-action"
              onClick={() => void copyImage()}
              title="Copy image"
              type="button"
            >
              <ImageIcon size={13} />
              <span className="artifact-inspector-action-label">
                {copied === "image" ? "Copied" : "Copy image"}
              </span>
            </button>
          ) : null}
          <button
            className="artifact-inspector-action"
            onClick={() => active && void downloadArtifact(ticket, active)}
            title="Download"
            type="button"
          >
            <Download size={13} />
            <span className="artifact-inspector-action-label">Download</span>
          </button>
          <span className="artifact-inspector-divider" aria-hidden="true" />
          <button
            aria-label="Close inspector"
            className="artifact-inspector-action is-icon is-close"
            onClick={onClose}
            title="Close (Esc)"
            type="button"
          >
            <X size={15} />
          </button>
        </div>
      </header>
      <div className="artifact-inspector-body" data-artifact-detail-kind={kind ?? undefined}>
        {detail ?? (
          <ArtifactFallback
            detail="This session did not deliver a renderable payload for this artifact."
            title="Artifact unavailable"
          />
        )}
      </div>
      <div className="artifact-inspector-hint" aria-hidden="true">
        esc to close{canPaginate ? " · ← → to navigate" : ""}
      </div>
    </div>
  );

  return createPortal(overlay, document.body);
}
