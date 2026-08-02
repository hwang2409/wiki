import { createPortal } from "react-dom";
import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode, RefObject } from "react";
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
import { VisualDiffArtifactDetail } from "./artifact-detail/visual-diff";
import { classifyArtifact, humanizeArtifactKind } from "./artifact-kind";
import { AudioRenderer, VideoRenderer } from "./artifact-media-renderers";
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
    || humanizeArtifactKind(event.artifact ? classifyArtifact(event.artifact) : undefined);
}

type InspectorScopeEntry = {
  element: () => HTMLElement | null;
  events: () => SessionEvent[];
  isOpen: () => boolean;
  open: (index: number) => void;
  panelFocusedTab: () => string | null;
  primary: boolean;
};

const inspectorScopes = new Set<InspectorScopeEntry>();

function paneAllows(element: HTMLElement | null): boolean {
  const pane = element?.closest(".pane-frame");
  return !pane || pane.classList.contains("is-focused");
}

/**
 * One listener serves every registered transcript scope. Resolution order:
 * a focused artifact wins, then a hovered one — each handled by the scope
 * that owns it, so sibling navigation stays within that transcript. With no
 * direct target, the primary scope (the main session transcript) falls back
 * to its panel-focused tab, then its most recent artifact.
 */
export function handleGlobalInspectKey(event: KeyboardEvent): void {
  if (event.defaultPrevented) return;
  const command = event.metaKey || event.ctrlKey;
  if (!command || event.shiftKey || event.altKey || event.key !== "Enter") return;
  if (isTextEntryTarget(event.target) || isTextEntryTarget(document.activeElement)) return;
  for (const scope of inspectorScopes) if (scope.isOpen()) return;
  const openIn = (scope: InspectorScopeEntry, index: number) => {
    event.preventDefault();
    scope.open(index);
  };
  const focusedHost = document.activeElement?.closest?.("[data-artifact-id]") ?? null;
  const hoveredHost = focusedHost
    ? null
    : Array.from(document.querySelectorAll<HTMLElement>("[data-artifact-id]"))
      .filter((element) => element.matches(":hover"))
      .at(-1) ?? null;
  const host = focusedHost ?? hoveredHost;
  if (host) {
    const artifactId = host.getAttribute("data-artifact-id");
    for (const scope of inspectorScopes) {
      const root = scope.element();
      if (!root || !root.contains(host) || !paneAllows(root)) continue;
      const index = scope.events().findIndex((candidate) => candidate.artifact_id === artifactId);
      if (index >= 0) {
        openIn(scope, index);
        return;
      }
    }
    return;
  }
  for (const scope of inspectorScopes) {
    if (!scope.primary || !paneAllows(scope.element())) continue;
    const events = scope.events();
    if (events.length === 0) continue;
    const tab = scope.panelFocusedTab();
    const tabIndex = tab ? events.findIndex((candidate) => candidate.artifact_id === tab) : -1;
    openIn(scope, tabIndex >= 0 ? tabIndex : events.length - 1);
    return;
  }
}

function registerInspectorScope(entry: InspectorScopeEntry): () => void {
  if (inspectorScopes.size === 0) {
    window.addEventListener("keydown", handleGlobalInspectKey);
  }
  inspectorScopes.add(entry);
  return () => {
    inspectorScopes.delete(entry);
    if (inspectorScopes.size === 0) {
      window.removeEventListener("keydown", handleGlobalInspectKey);
    }
  };
}

export function useArtifactInspector({
  getPanelFocusedTab,
  primary = false,
  scopeRef,
  ticket,
}: {
  getPanelFocusedTab?: () => string | null;
  primary?: boolean;
  scopeRef: RefObject<HTMLElement | null>;
  ticket: string;
}): {
  handleArtifactsChange: (events: SessionEvent[]) => void;
  inspector: ReactNode;
  inspectorOpen: boolean;
  openInspector: (event: SessionEvent) => void;
} {
  const [artifactEvents, setArtifactEvents] = useState<SessionEvent[]>([]);
  const [index, setIndex] = useState<number | null>(null);
  const eventsRef = useRef(artifactEvents);
  eventsRef.current = artifactEvents;
  const indexRef = useRef(index);
  indexRef.current = index;
  const getPanelFocusedTabRef = useRef(getPanelFocusedTab);
  getPanelFocusedTabRef.current = getPanelFocusedTab;

  useEffect(() => {
    if (index !== null && index >= artifactEvents.length) {
      setIndex(artifactEvents.length > 0 ? artifactEvents.length - 1 : null);
    }
  }, [artifactEvents.length, index]);

  useEffect(() => registerInspectorScope({
    element: () => scopeRef.current,
    events: () => eventsRef.current,
    isOpen: () => indexRef.current !== null,
    open: (next) => setIndex(next),
    panelFocusedTab: () => getPanelFocusedTabRef.current?.() ?? null,
    primary,
  }), [primary, scopeRef]);

  const handleArtifactsChange = useCallback((events: SessionEvent[]) => {
    setArtifactEvents(events);
  }, []);

  const openInspector = useCallback((event: SessionEvent) => {
    const next = eventsRef.current.findIndex(
      (candidate) => candidate.artifact_id === event.artifact_id,
    );
    if (next >= 0) setIndex(next);
  }, []);

  const inspector = index !== null && artifactEvents.length > 0 ? (
    <ArtifactInspector
      events={artifactEvents}
      index={Math.min(index, artifactEvents.length - 1)}
      onClose={() => setIndex(null)}
      onIndexChange={setIndex}
      ticket={ticket}
    />
  ) : null;

  return { handleArtifactsChange, inspector, inspectorOpen: index !== null, openInspector };
}

async function toPngBlob(blob: Blob): Promise<Blob> {
  if (blob.type === "image/png") return blob;
  const bitmap = await createImageBitmap(blob);
  try {
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("2d canvas context unavailable");
    context.drawImage(bitmap, 0, 0);
    const png = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, "image/png"));
    if (!png) throw new Error("PNG encode failed");
    return png;
  } finally {
    bitmap.close();
  }
}

// Chromium/WebKit only accept image/png ClipboardItems, so every payload is
// converted before the write. Failures surface as failures — never a silent
// URL-as-text copy behind a "Copied" label.
async function copyImagePayload(ticket: string, event: SessionEvent): Promise<boolean> {
  const artifact = event.artifact!;
  const source = artifact.data_base64
    ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
    : artifactUrl(ticket, event);
  if (typeof ClipboardItem === "undefined" || !navigator.clipboard?.write) return false;
  try {
    const response = await fetch(source);
    if (!response.ok) throw new Error(`Image fetch failed (${response.status})`);
    const png = await toPngBlob(await response.blob());
    await navigator.clipboard.write([new ClipboardItem({ "image/png": png })]);
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
  const previousArtifactId = useRef<string | null>(null);
  const [copied, setCopied] = useState<"source" | "image" | "image-failed" | null>(null);
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
    // Navigation can unmount the focused action (e.g. Copy image when leaving
    // an image artifact); reclaim focus so arrow keys keep working.
    if (previousArtifactId.current !== null) {
      const dialog = dialogRef.current;
      if (dialog && !dialog.contains(document.activeElement)) dialog.focus();
    }
    previousArtifactId.current = artifactId;
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

  const showCopied = useCallback((which: "source" | "image" | "image-failed") => {
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
    showCopied((await copyImagePayload(ticket, active)) ? "image" : "image-failed");
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
  const Icon = (kind && KIND_ICONS[kind]) || FileJson;
  const detail = active && artifact && kind ? (() => {
    switch (kind) {
      case "table": return <TableArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "image": return <ImageArtifactDetail artifact={artifact} event={active} onChange={onViewChange} state={viewState} ticket={ticket} />;
      case "mermaid": return <MermaidArtifactDetail onChange={onViewChange} source={artifact.source ?? ""} state={viewState} />;
      case "svg": return <SvgArtifactDetail onChange={onViewChange} source={artifact.source ?? ""} state={viewState} />;
      case "plot": return <PlotArtifactDetail key={active.artifact_id ?? undefined} spec={artifact.spec_vega_lite ?? {}} title={active.title ?? artifact.filename ?? null} />;
      case "diff": return <DiffArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "file-list": return <FileListArtifactDetail artifact={artifact} />;
      case "json": return <JsonArtifactDetail artifact={artifact} />;
      case "code": return <CodeArtifactDetail artifact={artifact} onChange={onViewChange} state={viewState} />;
      case "pdf": return <PdfArtifactDetail artifact={artifact} event={active} onChange={onViewChange} state={viewState} ticket={ticket} />;
      case "video": return <VideoRenderer artifact={artifact} event={active} ticket={ticket} />;
      case "audio": return <AudioRenderer artifact={artifact} event={active} ticket={ticket} />;
      case "visual-diff": return <VisualDiffArtifactDetail artifact={artifact} event={active} ticket={ticket} />;
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
          <span className="artifact-inspector-kind">{humanizeArtifactKind(kind ?? undefined)}</span>
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
          {kind !== "video" && kind !== "audio" ? (
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
          ) : null}
          {kind === "image" ? (
            <button
              className="artifact-inspector-action"
              onClick={() => void copyImage()}
              title="Copy image"
              type="button"
            >
              <ImageIcon size={13} />
              <span className="artifact-inspector-action-label">
                {copied === "image" ? "Copied" : copied === "image-failed" ? "Copy failed" : "Copy image"}
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
