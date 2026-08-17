import { useEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { Clock3, MoreHorizontal, X } from "lucide-react";
import type { SessionEvent } from "./api";
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
import { AudioRenderer, VideoRenderer } from "./artifact-media-renderers";
import { classifyArtifact, humanizeArtifactKind } from "./artifact-kind";
import { ArtifactFallback } from "./artifact-state";
import type { ArtifactViewState, PanelState } from "./transcript-store";

function tabLabelFor(event: SessionEvent | undefined): string {
  return event?.title || event?.artifact?.filename || humanizeArtifactKind(event?.artifact?.kind);
}


export function ArtifactPanel({
  artifacts,
  onClosePanel,
  onCloseTab,
  onFocusTab,
  onReopen,
  onResizeStart,
  onUpdateViewState,
  state,
  ticket,
  width,
}: {
  artifacts: Map<string, SessionEvent>;
  onClosePanel: () => void;
  onCloseTab: (artifactId: string) => void;
  onFocusTab: (artifactId: string) => void;
  onReopen: (artifactId: string) => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onUpdateViewState: (artifactId: string, state: ArtifactViewState) => void;
  state: PanelState;
  ticket: string;
  width: number;
}) {
  const [overflowOpen, setOverflowOpen] = useState(false);
  const overflowRef = useRef<HTMLDivElement | null>(null);
  const focusedId = state.focusedTab ?? state.tabs.at(-1) ?? null;
  const focusedEvent = focusedId ? artifacts.get(focusedId) : undefined;
  const artifact = focusedEvent?.artifact;

  useEffect(() => {
    if (!overflowOpen) return;
    function onDocDown(nativeEvent: MouseEvent) {
      if (overflowRef.current && !overflowRef.current.contains(nativeEvent.target as Node)) {
        setOverflowOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocDown);
    return () => document.removeEventListener("mousedown", onDocDown);
  }, [overflowOpen]);

  function onKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    const command = event.metaKey || event.ctrlKey;
    if (command && event.key.toLocaleLowerCase() === "w") {
      event.preventDefault();
      if (focusedId) onCloseTab(focusedId);
      return;
    }
    if (command && event.key === "0" && ["image", "svg", "mermaid", "pdf", "plot"].includes(artifact?.kind ?? "")) {
      event.preventDefault();
      event.currentTarget.querySelector<HTMLButtonElement>("[data-panel-reset-zoom]")?.click();
      return;
    }
    if (command && event.key.toLocaleLowerCase() === "f" && (artifact?.kind === "code" || artifact?.kind === "pdf")) {
      event.preventDefault();
      event.currentTarget.querySelector<HTMLButtonElement>("[data-code-find]")?.click();
    }
  }

  const viewState = focusedId ? state.viewState[focusedId] ?? {} : {};
  const detail = focusedEvent && artifact && focusedId ? (() => {
    const onChange = (next: ArtifactViewState) => onUpdateViewState(focusedId, next);
    switch (classifyArtifact(artifact)) {
      case "table": return <TableArtifactDetail artifact={artifact} onChange={onChange} state={viewState} />;
      case "image": return <ImageArtifactDetail artifact={artifact} event={focusedEvent} onChange={onChange} state={viewState} ticket={ticket} />;
      case "mermaid": return <MermaidArtifactDetail onChange={onChange} source={artifact.source ?? ""} state={viewState} />;
      case "svg": return <SvgArtifactDetail onChange={onChange} source={artifact.source ?? ""} state={viewState} />;
      case "plot": return <PlotArtifactDetail key={focusedId} spec={artifact.spec_vega_lite ?? {}} title={focusedEvent.title ?? artifact.filename ?? null} />;
      case "diff": return <DiffArtifactDetail artifact={artifact} onChange={onChange} state={viewState} />;
      case "file-list": return <FileListArtifactDetail artifact={artifact} />;
      case "json": return <JsonArtifactDetail artifact={artifact} />;
      case "code": return <CodeArtifactDetail artifact={artifact} onChange={onChange} state={viewState} />;
      case "pdf": return <PdfArtifactDetail artifact={artifact} event={focusedEvent} onChange={onChange} state={viewState} ticket={ticket} />;
      case "video": return <VideoRenderer artifact={artifact} event={focusedEvent} ticket={ticket} />;
      case "audio": return <AudioRenderer artifact={artifact} event={focusedEvent} ticket={ticket} />;
      case "visual-diff": return <VisualDiffArtifactDetail artifact={artifact} event={focusedEvent} ticket={ticket} />;
    }
  })() : null;

  return (
    <aside
      aria-label="Artifact panel"
      className="session-side-panel artifact-panel"
      data-focused-artifact={focusedId ?? undefined}
      style={{ width }}
      tabIndex={-1}
      onKeyDown={onKeyDown}
    >
      <div aria-label="Resize artifact panel" className="session-resize" onPointerDown={onResizeStart} />
      <div className="artifact-panel-chrome">
        <div className="artifact-panel-tabs" role="tablist" aria-label="Open artifacts">
          {state.tabs.map((artifactId) => {
            const event = artifacts.get(artifactId);
            const selected = artifactId === focusedId;
            const label = tabLabelFor(event);
            return (
              <div
                className={`bb-tab-pill bb-tab-pill--closable${selected ? " bb-tab-pill--active" : ""}`}
                key={artifactId}
              >
                <button
                  aria-selected={selected}
                  className="bb-tab-pill__button"
                  role="tab"
                  title={label}
                  type="button"
                  onClick={() => onFocusTab(artifactId)}
                >
                  <span className="bb-tab-pill__label">{label}</span>
                </button>
                <button
                  aria-label={`Close ${label}`}
                  className="bb-tab-pill__close"
                  type="button"
                  onClick={() => onCloseTab(artifactId)}
                >
                  <X aria-hidden="true" className="bb-tab-pill__close-glyph" />
                </button>
              </div>
            );
          })}
        </div>
        <div className="artifact-panel-actions">
          <div className="artifact-panel-overflow" ref={overflowRef}>
            <button
              aria-controls="artifact-panel-overflow-menu"
              aria-expanded={overflowOpen}
              aria-label="Artifact panel menu"
              className="bb-icon-button"
              type="button"
              onClick={() => setOverflowOpen((value) => !value)}
            >
              <MoreHorizontal aria-hidden="true" />
            </button>
            {overflowOpen ? (
              <div className="artifact-panel-overflow-menu" id="artifact-panel-overflow-menu" role="menu">
                {state.recentlyClosed.length > 0 ? (
                  <>
                    <div className="artifact-panel-overflow-heading" role="presentation">
                      <Clock3 aria-hidden="true" size={11} />
                      <span>Recently closed</span>
                    </div>
                    {state.recentlyClosed.map((artifactId) => (
                      <button
                        className="artifact-panel-overflow-item"
                        key={artifactId}
                        role="menuitem"
                        type="button"
                        onClick={() => {
                          setOverflowOpen(false);
                          onReopen(artifactId);
                        }}
                      >
                        {tabLabelFor(artifacts.get(artifactId))}
                      </button>
                    ))}
                  </>
                ) : (
                  <div className="artifact-panel-overflow-empty" role="presentation">No recently closed artifacts.</div>
                )}
              </div>
            ) : null}
          </div>
          <button
            aria-label="Close artifact panel"
            className="bb-icon-button"
            type="button"
            onClick={onClosePanel}
          >
            <X aria-hidden="true" />
          </button>
        </div>
      </div>
      <div className="artifact-panel-detail" data-artifact-detail-kind={artifact?.kind}>
        {detail ?? (
          <div className="artifact-panel-missing">
            <ArtifactFallback
              actions={
                focusedId ? (
                  <button
                    className="artifact-render-fallback-action"
                    type="button"
                    onClick={() => onCloseTab(focusedId)}
                  >
                    Close tab
                  </button>
                ) : null
              }
              detail="This session did not deliver a renderable payload for this artifact."
              title="Artifact unavailable"
            />
          </div>
        )}
      </div>
    </aside>
  );
}
