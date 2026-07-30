import {
  useEffect,
  useCallback,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { Bot, GitBranch, GitPullRequest, RefreshCw, X } from "lucide-react";
import { AgentPrReviewPanel } from "./agent-pr-review";
import { LoopStateChrome } from "./loop-state-chrome";
import { WorkgraphPanel } from "./workgraph-panel";
import { ArtifactPanel } from "./artifact-panel";
import { deletePaneStateEntries } from "./pane-state-cache";
import { ReplaceAgentModal } from "./replace-agent-modal";
import { getTicketCosts, type CostRow, type SessionEvent, type SpawnWorkerEffort, type SpawnWorkerKind } from "./api";
import { SessionTab, usePollTick } from "./session";
import { StatusBadge } from "./status-badge";
import {
  readPanelState,
  writePanelState,
  type ArtifactViewState,
  type PanelState,
} from "./transcript-store";

const SIDE_PANEL_WIDTH_KEY = "wiki-session-side-panel-width";
const MIN_PANEL_WIDTH = 320;
const ARTIFACT_PANEL_WIDTH_KEY = "wiki-artifact-panel-width";

function clampPanelWidth(width: number, containerWidth: number) {
  return Math.min(Math.max(width, MIN_PANEL_WIDTH), Math.round(containerWidth * 0.7));
}

function panelStateFromUrl(ticket: string, current: PanelState, closeWhenAbsent = true): PanelState {
  const params = new URL(window.location.href).searchParams;
  if (params.get("panel") !== ticket) return closeWhenAbsent ? { ...current, open: false } : current;
  const listed = (params.get("tab") ?? "").split(",").filter(Boolean);
  const artifact = params.get("artifact");
  const focus = params.get("focus") ?? artifact;
  const tabs = [...new Set([...listed, ...(artifact ? [artifact] : [])])];
  if (tabs.length === 0) return { ...current, open: false };
  return {
    ...current,
    focusedTab: focus && tabs.includes(focus) ? focus : tabs.at(-1) ?? null,
    open: true,
    tabs,
  };
}

function writePanelUrl(ticket: string, state: PanelState, replace = false) {
  const url = new URL(window.location.href);
  if (state.open && state.tabs.length > 0) {
    const focus = state.focusedTab ?? state.tabs.at(-1) ?? "";
    url.searchParams.set("panel", ticket);
    url.searchParams.set("artifact", focus);
    url.searchParams.set("tab", state.tabs.join(","));
    url.searchParams.set("focus", focus);
  } else if (url.searchParams.get("panel") === ticket) {
    url.searchParams.delete("panel");
    url.searchParams.delete("artifact");
    url.searchParams.delete("tab");
    url.searchParams.delete("focus");
  }
  window.history[replace ? "replaceState" : "pushState"](null, "", url);
}

type SidePanelState =
  | { kind: "review" }
  | { kind: "graph" }
  | { kind: "subagent"; subagent: string }
  | null;

type SurfaceState = {
  panel: SidePanelState;
  panelWidth: number;
};

export type AgentRoutePanel = "review" | "graph" | null;

export type AgentSessionSurfaceWorker = {
  ticket: string;
  kind?: string | null;
  role?: string | null;
  model?: string | null;
  effort?: SpawnWorkerEffort | null;
  pr?: string | null;
  canReview?: boolean;
  canReplace?: boolean;
};

function formatTicketCost(row: CostRow): string {
  if (row.cost_usd === null) return `unpriced · ${row.unpriced_tokens.toLocaleString()} tokens`;
  if (row.pricing === "mixed") return `$${row.cost_usd.toFixed(4)} + unpriced`;
  return `$${row.cost_usd.toFixed(4)}`;
}

function TicketCostStrip({ ticket, refreshTick }: { ticket: string; refreshTick: number }) {
  const [summary, setSummary] = useState<CostRow | null>(null);
  useEffect(() => {
    let active = true;
    getTicketCosts(ticket)
      .then((data) => {
        if (active) setSummary(data.totals);
      })
      .catch(() => {
        if (active) setSummary(null);
      });
    return () => {
      active = false;
    };
  }, [ticket, refreshTick]);
  if (!summary || summary.total_tokens === 0) return null;
  return (
    <div className="ticket-cost-strip" aria-label={`cost for ${ticket}`}>
      <span>cost</span>
      <strong className={`is-${summary.pricing}`}>{formatTicketCost(summary)}</strong>
      <span className="ticket-cost-detail">{summary.total_tokens.toLocaleString()} tokens</span>
      {summary.models.length > 0 ? <span className="ticket-cost-detail">{summary.models.join(", ")}</span> : null}
    </div>
  );
}

const surfaceStateCache = new Map<string, SurfaceState>();

export function clearSurfacePaneState(paneStateKey: string) {
  deletePaneStateEntries(surfaceStateCache, paneStateKey);
}

function readSurfaceState(
  key: string,
  canReview: boolean,
  initialPanel: AgentRoutePanel,
): SurfaceState {
  const cached = surfaceStateCache.get(key) ?? null;
  const fallbackWidth = clampPanelWidth(
    Number(localStorage.getItem(SIDE_PANEL_WIDTH_KEY)) || 480,
    window.innerWidth
  );
  const cachedPanel =
    cached?.panel?.kind === "review" && !canReview ? null : cached?.panel ?? null;
  return {
    panel:
      initialPanel === "review" && canReview
        ? { kind: "review" }
        : initialPanel === "graph"
          ? { kind: "graph" }
          : cachedPanel,
    panelWidth: cached?.panelWidth ?? fallbackWidth,
  };
}

function SessionSidePanel({
  badge,
  children,
  className,
  icon,
  onClose,
  onResizeStart,
  title,
  width,
}: {
  badge?: string;
  children: ReactNode;
  className?: string;
  icon: ReactNode;
  onClose: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  title: string;
  width: number;
}) {
  return (
    <aside
      className={`session-side-panel${className ? ` ${className}` : ""}`}
      style={{ width }}
    >
      <div className="session-resize" onPointerDown={onResizeStart} />
      <header className="session-header">
        {icon}
        <span className="session-ticket">{title}</span>
        {badge ? <StatusBadge compact label={badge} state="faint" /> : null}
        <button
          aria-label="Close"
          className="session-close"
          type="button"
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </header>
      {children}
    </aside>
  );
}

function ReviewSidePanel({
  canApprove,
  onClose,
  onResizeStart,
  ticket,
  tick,
  width,
}: {
  canApprove: boolean;
  onClose: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  ticket: string;
  tick: number;
  width: number;
}) {
  return (
    <SessionSidePanel
      badge="pull request"
      className="session-side-panel-review"
      icon={<GitPullRequest size={13} />}
      onClose={onClose}
      onResizeStart={onResizeStart}
      title={ticket}
      width={width}
    >
      <AgentPrReviewPanel canApprove={canApprove} ticket={ticket} tick={tick} />
    </SessionSidePanel>
  );
}

function WorkgraphSidePanel({
  onClose,
  onResizeStart,
  ticket,
  tick,
  width,
}: {
  onClose: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  ticket: string;
  tick: number;
  width: number;
}) {
  return (
    <SessionSidePanel
      badge="workgraph"
      className="session-side-panel-graph"
      icon={<GitBranch size={13} />}
      onClose={onClose}
      onResizeStart={onResizeStart}
      title={ticket}
      width={width}
    >
      <WorkgraphPanel ticket={ticket} tick={tick} />
    </SessionSidePanel>
  );
}

function SubagentSidePanel({
  onClose,
  onResizeStart,
  stateKey,
  subagent,
  ticket,
  width,
}: {
  onClose: () => void;
  onResizeStart: (event: ReactPointerEvent<HTMLDivElement>) => void;
  stateKey: string;
  subagent: string;
  ticket: string;
  width: number;
}) {
  return (
    <SessionSidePanel
      badge="read-only"
      className="session-side-panel-subagent"
      icon={<Bot size={13} />}
      onClose={onClose}
      onResizeStart={onResizeStart}
      title={`subagent ${subagent.slice(0, 8)}`}
      width={width}
    >
      <SessionTab showComposer={false} stateKey={stateKey} subagent={subagent} ticket={ticket} />
    </SessionSidePanel>
  );
}

export function AgentSessionSurface({
  context,
  initialPanel = null,
  onClose,
  refreshTick,
  stateKey,
  worker,
}: {
  context: "full" | "pane";
  initialPanel?: AgentRoutePanel;
  onClose?: () => void;
  refreshTick: number;
  stateKey?: string;
  worker: AgentSessionSurfaceWorker;
}) {
  const tick = usePollTick(refreshTick);
  const canReview = worker.canReview ?? Boolean(worker.pr);
  const rowRef = useRef<HTMLDivElement | null>(null);
  const surfaceStateKey = `${stateKey ?? worker.ticket}:${worker.ticket}`;
  const sessionPanelKey = `${worker.ticket}:main`;
  const [panelWidth, setPanelWidth] = useState(() =>
    readSurfaceState(surfaceStateKey, canReview, initialPanel).panelWidth
  );
  const [panel, setPanel] = useState<SidePanelState>(
    () => readSurfaceState(surfaceStateKey, canReview, initialPanel).panel
  );
  const [artifactWidth, setArtifactWidth] = useState(() =>
    clampPanelWidth(
      Number(localStorage.getItem(ARTIFACT_PANEL_WIDTH_KEY)) || Math.round(window.innerWidth * 0.45),
      window.innerWidth,
    )
  );
  const [panelState, setPanelState] = useState<PanelState>(() =>
    panelStateFromUrl(worker.ticket, readPanelState(sessionPanelKey), false)
  );
  const panelStateRef = useRef(panelState);
  panelStateRef.current = panelState;
  const [artifacts, setArtifacts] = useState<Map<string, SessionEvent>>(new Map());
  const [replaceOpen, setReplaceOpen] = useState(false);
  const commitPanelState = useCallback((update: (current: PanelState) => PanelState, syncUrl = true) => {
    const next = update(panelStateRef.current);
    panelStateRef.current = next;
    setPanelState(next);
    writePanelState(sessionPanelKey, next);
    if (syncUrl) writePanelUrl(worker.ticket, next);
    return next;
  }, [sessionPanelKey, worker.ticket]);
  const inspectSubagent = useCallback(
    (subagent: string) => {
      commitPanelState((current) => ({ ...current, open: false }));
      setPanel({ kind: "subagent", subagent });
    },
    [commitPanelState]
  );

  const openArtifact = useCallback((event: SessionEvent) => {
    const artifactId = event.artifact_id;
    if (!artifactId) return;
    setPanel(null);
    if (!localStorage.getItem(ARTIFACT_PANEL_WIDTH_KEY)) {
      const containerWidth = rowRef.current?.getBoundingClientRect().width ?? window.innerWidth;
      setArtifactWidth(clampPanelWidth(Math.round(containerWidth * 0.45), containerWidth));
    }
    setArtifacts((current) => new Map(current).set(artifactId, event));
    commitPanelState((current) => {
      const tabs = current.tabs.includes(artifactId) ? current.tabs : [...current.tabs, artifactId];
      return {
        ...current,
        focusedTab: artifactId,
        open: true,
        recentlyClosed: current.recentlyClosed.filter((id) => id !== artifactId),
        tabs,
      };
    });
    window.requestAnimationFrame(() => rowRef.current?.querySelector<HTMLElement>(".artifact-panel")?.focus());
  }, [commitPanelState]);

  const closeArtifactTab = useCallback((artifactId: string) => {
    commitPanelState((current) => {
      const index = current.tabs.indexOf(artifactId);
      const tabs = current.tabs.filter((id) => id !== artifactId);
      const focusedTab = current.focusedTab === artifactId
        ? tabs[Math.min(Math.max(index, 0), tabs.length - 1)] ?? null
        : current.focusedTab;
      return {
        ...current,
        focusedTab,
        open: tabs.length > 0 && current.open,
        recentlyClosed: [artifactId, ...current.recentlyClosed.filter((id) => id !== artifactId)].slice(0, 10),
        tabs,
      };
    });
  }, [commitPanelState]);

  const focusArtifactTab = useCallback((artifactId: string) => {
    commitPanelState((current) => {
      if (!current.tabs.includes(artifactId)) return current;
      return { ...current, focusedTab: artifactId, open: true };
    });
  }, [commitPanelState]);

  const reopenArtifact = useCallback((artifactId: string) => {
    commitPanelState((current) => {
      return {
        ...current,
        focusedTab: artifactId,
        open: true,
        recentlyClosed: current.recentlyClosed.filter((id) => id !== artifactId),
        tabs: current.tabs.includes(artifactId) ? current.tabs : [...current.tabs, artifactId],
      };
    });
  }, [commitPanelState]);

  const closeArtifactPanel = useCallback(() => {
    commitPanelState((current) => ({ ...current, open: false }));
  }, [commitPanelState]);

  const updateArtifactViewState = useCallback((artifactId: string, viewState: ArtifactViewState) => {
    commitPanelState((current) => ({
      ...current,
      viewState: { ...current.viewState, [artifactId]: viewState },
    }), false);
  }, [commitPanelState]);

  useEffect(() => {
    const stored = readSurfaceState(surfaceStateKey, canReview, initialPanel);
    setPanelWidth(stored.panelWidth);
    setPanel(stored.panel);
  }, [canReview, initialPanel, surfaceStateKey]);

  useEffect(() => {
    if (initialPanel === "review" && canReview) setPanel({ kind: "review" });
    if (initialPanel === "graph") setPanel({ kind: "graph" });
  }, [canReview, initialPanel]);

  useEffect(() => {
    if (!canReview && panel?.kind === "review") setPanel(null);
  }, [canReview, panel]);

  useEffect(() => {
    surfaceStateCache.set(surfaceStateKey, {
      panel: !canReview && panel?.kind === "review" ? null : panel,
      panelWidth,
    });
  }, [canReview, panel, panelWidth, surfaceStateKey]);

  useEffect(() => {
    const next = panelStateFromUrl(worker.ticket, readPanelState(sessionPanelKey), false);
    panelStateRef.current = next;
    setPanelState(next);
    writePanelState(sessionPanelKey, next);
    if (next.open) setPanel(null);
  }, [sessionPanelKey, worker.ticket]);

  useEffect(() => {
    const onPopState = () => {
      const next = panelStateFromUrl(worker.ticket, readPanelState(sessionPanelKey));
      panelStateRef.current = next;
      setPanelState(next);
      writePanelState(sessionPanelKey, next);
      if (next.open) setPanel(null);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [sessionPanelKey, worker.ticket]);

  useEffect(() => {
    const onShortcut = (event: KeyboardEvent) => {
      const pane = rowRef.current?.closest(".pane-frame");
      if (pane && !pane.classList.contains("is-focused")) return;
      if (event.key === "Escape" && panelState.open && panelState.focusedTab) {
        event.preventDefault();
        closeArtifactTab(panelState.focusedTab);
        return;
      }
      const command = event.metaKey || event.ctrlKey;
      if (!command || !event.shiftKey) return;
      if (event.key.toLocaleLowerCase() === "a") {
        if (panelState.tabs.length === 0) return;
        event.preventDefault();
        setPanel(null);
        commitPanelState((current) => ({
          ...current,
          open: !current.open,
          focusedTab: current.focusedTab ?? current.tabs.at(-1) ?? null,
        }));
        return;
      }
      if (!panelState.open || panelState.tabs.length < 2) return;
      const offset = event.code === "BracketLeft" ? -1 : event.code === "BracketRight" ? 1 : 0;
      if (!offset) return;
      event.preventDefault();
      commitPanelState((current) => {
        const index = Math.max(0, current.tabs.indexOf(current.focusedTab ?? ""));
        const focusedTab = current.tabs[(index + offset + current.tabs.length) % current.tabs.length] ?? null;
        return { ...current, focusedTab };
      });
    };
    window.addEventListener("keydown", onShortcut);
    return () => window.removeEventListener("keydown", onShortcut);
  }, [closeArtifactTab, commitPanelState, panelState.focusedTab, panelState.open, panelState.tabs]);

  const handleArtifactsChange = useCallback((events: SessionEvent[]) => {
    setArtifacts(
      new Map(events.flatMap((event) => (event.artifact_id ? [[event.artifact_id, event] as const] : [])))
    );
  }, []);

  const resizePanel = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const onMove = (move: PointerEvent) => {
      const rect = rowRef.current?.getBoundingClientRect();
      const containerWidth = rect?.width ?? window.innerWidth;
      const rightEdge = rect?.right ?? window.innerWidth;
      setPanelWidth(clampPanelWidth(rightEdge - move.clientX, containerWidth));
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      setPanelWidth((current) => {
        localStorage.setItem(SIDE_PANEL_WIDTH_KEY, String(current));
        return current;
      });
    };
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* synthetic events lack a real pointer — listeners still work */
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  };

  const resizeArtifactPanel = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const handle = event.currentTarget;
    const onMove = (move: PointerEvent) => {
      const rect = rowRef.current?.getBoundingClientRect();
      const containerWidth = rect?.width ?? window.innerWidth;
      const rightEdge = rect?.right ?? window.innerWidth;
      setArtifactWidth(clampPanelWidth(rightEdge - move.clientX, containerWidth));
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      setArtifactWidth((current) => {
        localStorage.setItem(ARTIFACT_PANEL_WIDTH_KEY, String(current));
        return current;
      });
    };
    try {
      handle.setPointerCapture(event.pointerId);
    } catch {
      /* synthetic events lack a real pointer — listeners still work */
    }
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
  };

  return (
    <div className={`agent-session-surface-row is-${context}`} ref={rowRef}>
      <section className={`agent-session-surface is-${context}`}>
        <header className="session-header agent-session-surface-head">
          <span className="session-ticket">{worker.ticket}</span>
          {worker.kind ? <StatusBadge compact label={worker.kind} state="neutral" /> : null}
          {worker.role ? <StatusBadge compact label={worker.role} state="neutral" /> : null}
          {worker.model ? <StatusBadge compact label={worker.model} state="faint" /> : null}
          <LoopStateChrome ticket={worker.ticket} tick={tick} />
          <div className="agent-surface-actions">
              {worker.canReplace && worker.kind && worker.model ? (
                <button
                  className="agent-surface-action"
                  type="button"
                  onClick={() => setReplaceOpen(true)}
                >
                  <RefreshCw size={13} />
                  Replace
                </button>
              ) : null}
              {canReview ? (
                <button
                  className={`agent-surface-action${panel?.kind === "review" ? " is-active" : ""}`}
                  type="button"
                  onClick={() => {
                    if (panelState.open) closeArtifactPanel();
                    setPanel((current) => current?.kind === "review" ? null : { kind: "review" });
                  }}
                >
                  <GitPullRequest size={13} />
                  Review
                </button>
              ) : null}
              <button
                className={`agent-surface-action${panel?.kind === "graph" ? " is-active" : ""}`}
                type="button"
                onClick={() => {
                  if (panelState.open) closeArtifactPanel();
                  setPanel((current) => (current?.kind === "graph" ? null : { kind: "graph" }));
                }}
              >
                <GitBranch size={13} />
                Graph
              </button>
              {onClose ? (
                <button
                  aria-label="Close pane"
                  className="session-close"
                  type="button"
                  onClick={onClose}
                >
                  <X size={14} />
                </button>
              ) : null}
            </div>
        </header>
        <TicketCostStrip ticket={worker.ticket} refreshTick={tick} />
        <div className="agent-session-surface-main">
          <SessionTab
            onArtifactsChange={handleArtifactsChange}
            onInspect={inspectSubagent}
            onOpenArtifact={openArtifact}
            stateKey={`${surfaceStateKey}:main`}
            ticket={worker.ticket}
          />
        </div>
      </section>
      {panelState.open && panelState.tabs.length > 0 ? (
        <ArtifactPanel
          artifacts={artifacts}
          onClosePanel={closeArtifactPanel}
          onCloseTab={closeArtifactTab}
          onFocusTab={focusArtifactTab}
          onReopen={reopenArtifact}
          onResizeStart={resizeArtifactPanel}
          onUpdateViewState={updateArtifactViewState}
          state={panelState}
          ticket={worker.ticket}
          width={artifactWidth}
        />
      ) : panel?.kind === "review" ? (
        <ReviewSidePanel
          canApprove={canReview}
          onClose={() => setPanel(null)}
          onResizeStart={resizePanel}
          ticket={worker.ticket}
          tick={tick}
          width={panelWidth}
        />
      ) : panel?.kind === "graph" ? (
        <WorkgraphSidePanel
          onClose={() => setPanel(null)}
          onResizeStart={resizePanel}
          ticket={worker.ticket}
          tick={tick}
          width={panelWidth}
        />
      ) : panel?.kind === "subagent" ? (
        <SubagentSidePanel
          onClose={() => setPanel(null)}
          onResizeStart={resizePanel}
          stateKey={`${surfaceStateKey}:subagent:${panel.subagent}`}
          subagent={panel.subagent}
          ticket={worker.ticket}
          width={panelWidth}
        />
      ) : null}
      {replaceOpen && worker.kind && worker.model ? (
        <ReplaceAgentModal
          target={{
            id: worker.ticket,
            kind: worker.kind as SpawnWorkerKind,
            model: worker.model,
            effort: worker.effort,
            role: worker.role,
          }}
          onClose={() => setReplaceOpen(false)}
        />
      ) : null}
    </div>
  );
}

export function AgentSessionView({
  initialPanel = null,
  refreshTick,
  stateKey,
  worker,
}: {
  initialPanel?: AgentRoutePanel;
  refreshTick: number;
  stateKey?: string;
  worker: AgentSessionSurfaceWorker;
}) {
  return (
    <AgentSessionSurface
      context="full"
      initialPanel={initialPanel}
      refreshTick={refreshTick}
      stateKey={stateKey}
      worker={worker}
    />
  );
}
