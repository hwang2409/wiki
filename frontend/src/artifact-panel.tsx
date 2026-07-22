import { useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from "react";
import { Clock3, MoreHorizontal, Pin, X } from "lucide-react";
import type { SessionEvent } from "./api";
import { CodeArtifactDetail } from "./artifact-detail/code";
import { DiffArtifactDetail } from "./artifact-detail/diff";
import { FileListArtifactDetail } from "./artifact-detail/file-list";
import { ImageArtifactDetail } from "./artifact-detail/image";
import { JsonArtifactDetail } from "./artifact-detail/json";
import { MermaidArtifactDetail } from "./artifact-detail/mermaid";
import { PlotArtifactDetail } from "./artifact-detail/plot";
import { SvgArtifactDetail } from "./artifact-detail/svg";
import { TableArtifactDetail } from "./artifact-detail/table";
import { classifyArtifact } from "./artifact-kind";
import { StatusBadge } from "./status-badge";
import type { ArtifactViewState, PanelState } from "./transcript-store";

function titleFor(event: SessionEvent | undefined, id: string) {
  return event?.title || event?.artifact?.filename || event?.artifact?.kind || `artifact ${id.slice(0, 8)}`;
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
  const [openMenu, setOpenMenu] = useState<string | null>(null);
  const focusedId = state.focusedTab ?? state.tabs.at(-1) ?? null;
  const focusedEvent = focusedId ? artifacts.get(focusedId) : undefined;
  const artifact = focusedEvent?.artifact;

  function onKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    const command = event.metaKey || event.ctrlKey;
    if (command && event.key.toLocaleLowerCase() === "w") {
      event.preventDefault();
      if (focusedId) onCloseTab(focusedId);
      return;
    }
    if (command && event.key === "0" && ["image", "svg", "mermaid"].includes(artifact?.kind ?? "")) {
      event.preventDefault();
      event.currentTarget.querySelector<HTMLButtonElement>("[data-panel-reset-zoom]")?.click();
      return;
    }
    if (command && event.key.toLocaleLowerCase() === "f" && artifact?.kind === "code") {
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
      case "plot": return <PlotArtifactDetail spec={artifact.spec_vega_lite ?? {}} />;
      case "diff": return <DiffArtifactDetail artifact={artifact} />;
      case "file-list": return <FileListArtifactDetail artifact={artifact} />;
      case "json": return <JsonArtifactDetail artifact={artifact} />;
      case "code": return <CodeArtifactDetail artifact={artifact} onChange={onChange} state={viewState} />;
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
      <div className="artifact-panel-tabs" role="tablist" aria-label="Open artifacts">
        {state.tabs.map((artifactId) => {
          const event = artifacts.get(artifactId);
          const selected = artifactId === focusedId;
          return (
            <div className={`artifact-panel-tab${selected ? " is-active" : ""}`} key={artifactId}>
              <button
                aria-selected={selected}
                className="artifact-panel-tab-main"
                role="tab"
                title={titleFor(event, artifactId)}
                type="button"
                onClick={() => onFocusTab(artifactId)}
              >
                <span>{titleFor(event, artifactId)}</span>
                {event?.artifact?.kind ? (
                  <StatusBadge
                    className="artifact-panel-tab-kind"
                    compact
                    label={event.artifact.kind}
                    state="faint"
                  />
                ) : null}
              </button>
              <button aria-label={`Artifact menu for ${titleFor(event, artifactId)}`} className="artifact-panel-tab-action" type="button" onClick={() => setOpenMenu((current) => current === artifactId ? null : artifactId)}><MoreHorizontal size={12} /></button>
              {openMenu === artifactId ? (
                <div className="artifact-panel-tab-menu" role="menu">
                  <button disabled title="Coming soon" type="button" role="menuitem"><Pin size={11} /> Pin to vault <span>Coming soon</span></button>
                </div>
              ) : null}
              <button aria-label={`Close ${titleFor(event, artifactId)}`} className="artifact-panel-tab-action" type="button" onClick={() => onCloseTab(artifactId)}><X size={12} /></button>
            </div>
          );
        })}
        <button aria-label="Close artifact panel" className="artifact-panel-close" type="button" onClick={onClosePanel}><X size={14} /></button>
      </div>
      {state.recentlyClosed.length > 0 ? (
        <div className="artifact-panel-recent" aria-label="Recently closed artifacts">
          <Clock3 size={11} />
          <span>Recently closed</span>
          {state.recentlyClosed.map((artifactId) => (
            <button key={artifactId} type="button" onClick={() => onReopen(artifactId)}>{titleFor(artifacts.get(artifactId), artifactId)}</button>
          ))}
        </div>
      ) : null}
      <div className="artifact-panel-detail" data-artifact-detail-kind={artifact?.kind}>
        {detail ?? <div className="artifact-panel-missing">Artifact payload is not available in this session.</div>}
      </div>
    </aside>
  );
}
