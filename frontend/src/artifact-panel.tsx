import { useEffect, useId, useRef, useState } from "react";
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
import { useMenuKeyboard } from "./use-menu-keyboard";

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
  const overflowTriggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLElement | null>(null);
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const detailId = useId();
  const overflowMenuId = "artifact-panel-overflow-menu";
  const { menuRef: overflowMenuRef, onKeyDown: onOverflowKeyDown } = useMenuKeyboard({
    fallbackRef: panelRef,
    open: overflowOpen,
    onClose: () => setOverflowOpen(false),
    triggerRef: overflowTriggerRef,
  });
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

  function closeTab(artifactId: string) {
    const index = state.tabs.indexOf(artifactId);
    if (index < 0) return;
    const shouldMoveFocus = artifactId === focusedId;
    const nextTabId = state.tabs[index + 1] ?? state.tabs[index - 1] ?? null;
    onCloseTab(artifactId);
    if (!shouldMoveFocus) return;
    window.requestAnimationFrame(() => {
      const nextTab = nextTabId ? tabRefs.current.get(nextTabId) : null;
      if (nextTab?.isConnected) nextTab.focus();
      else panelRef.current?.focus();
    });
  }

  function onTabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, artifactId: string) {
    const index = state.tabs.indexOf(artifactId);
    if (index < 0 || state.tabs.length === 0) return;
    let nextIndex: number | null = null;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (index + 1) % state.tabs.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (index - 1 + state.tabs.length) % state.tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = state.tabs.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onFocusTab(artifactId);
      return;
    }
    if (nextIndex === null) return;
    event.preventDefault();
    const nextTabId = state.tabs[nextIndex];
    if (!nextTabId) return;
    onFocusTab(nextTabId);
    window.requestAnimationFrame(() => tabRefs.current.get(nextTabId)?.focus());
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    const command = event.metaKey || event.ctrlKey;
    if (event.key === "Escape" && focusedId) {
      event.preventDefault();
      closeTab(focusedId);
      return;
    }
    if (command && event.key.toLocaleLowerCase() === "w") {
      event.preventDefault();
      if (focusedId) closeTab(focusedId);
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
      ref={panelRef}
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
                  aria-controls={detailId}
                  className="bb-tab-pill__button"
                  id={`${detailId}-tab-${artifactId}`}
                  role="tab"
                  ref={(button) => {
                    if (button) tabRefs.current.set(artifactId, button);
                    else tabRefs.current.delete(artifactId);
                  }}
                  tabIndex={selected ? 0 : -1}
                  title={label}
                  type="button"
                  onKeyDown={(event) => onTabKeyDown(event, artifactId)}
                  onClick={() => onFocusTab(artifactId)}
                >
                  <span className="bb-tab-pill__label">{label}</span>
                </button>
                <button
                  aria-label={`Close ${label}`}
                  className="bb-tab-pill__close"
                  type="button"
                  onMouseDown={(event) => {
                    if (artifactId !== focusedId) event.preventDefault();
                  }}
                  onClick={() => closeTab(artifactId)}
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
              aria-controls={overflowMenuId}
              aria-expanded={overflowOpen}
              aria-haspopup="menu"
              aria-label="Artifact panel menu"
              className="bb-icon-button"
              ref={overflowTriggerRef}
              type="button"
              onClick={() => setOverflowOpen((value) => !value)}
            >
              <MoreHorizontal aria-hidden="true" />
            </button>
            {overflowOpen ? (
              <div
                className="artifact-panel-overflow-menu"
                id={overflowMenuId}
                ref={overflowMenuRef}
                role="menu"
                onKeyDown={onOverflowKeyDown}
              >
                {state.recentlyClosed.length > 0 ? (
                  <>
                    <div className="artifact-panel-overflow-heading" role="presentation">
                      <Clock3 aria-hidden="true" size={11} />
                      <span>Recently closed</span>
                    </div>
                    {state.recentlyClosed.map((artifactId, index) => (
                      <button
                        className="artifact-panel-overflow-item"
                        key={artifactId}
                        role="menuitem"
                        tabIndex={index === 0 ? 0 : -1}
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
      <div
        aria-labelledby={focusedId ? `${detailId}-tab-${focusedId}` : undefined}
        className="artifact-panel-detail"
        data-artifact-detail-kind={artifact?.kind}
        id={detailId}
        role="tabpanel"
        tabIndex={-1}
      >
        {detail ?? (
          <div className="artifact-panel-missing">
            <ArtifactFallback
              actions={
                focusedId ? (
                  <button
                    className="artifact-render-fallback-action"
                    type="button"
                    onClick={() => closeTab(focusedId)}
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
