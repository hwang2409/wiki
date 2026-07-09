import {
  useEffect,
  useCallback,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { Bot, GitPullRequest, X } from "lucide-react";
import { AgentPrReviewPanel } from "./agent-pr-review";
import { SessionTab, usePollTick } from "./session";

const SIDE_PANEL_WIDTH_KEY = "wiki-session-side-panel-width";
const MIN_PANEL_WIDTH = 320;

function clampPanelWidth(width: number, containerWidth: number) {
  return Math.min(Math.max(width, MIN_PANEL_WIDTH), Math.round(containerWidth * 0.7));
}

type SidePanelState =
  | { kind: "review" }
  | { kind: "subagent"; subagent: string }
  | null;

type SurfaceState = {
  panel: SidePanelState;
  panelWidth: number;
};

export type AgentRoutePanel = "review" | null;

export type AgentSessionSurfaceWorker = {
  ticket: string;
  kind?: string | null;
  role?: string | null;
  model?: string | null;
  pr?: string | null;
  canReview?: boolean;
};

const surfaceStateCache = new Map<string, SurfaceState>();

export function clearSurfacePaneState(paneStateKey: string) {
  const prefix = `${paneStateKey}:`;
  for (const key of [...surfaceStateCache.keys()]) {
    if (key.startsWith(prefix)) surfaceStateCache.delete(key);
  }
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
        {badge ? <span className="agent-chip is-faint">{badge}</span> : null}
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
  const [panelWidth, setPanelWidth] = useState(() =>
    readSurfaceState(surfaceStateKey, canReview, initialPanel).panelWidth
  );
  const [panel, setPanel] = useState<SidePanelState>(
    () => readSurfaceState(surfaceStateKey, canReview, initialPanel).panel
  );
  const inspectSubagent = useCallback(
    (subagent: string) => setPanel({ kind: "subagent", subagent }),
    []
  );

  useEffect(() => {
    const stored = readSurfaceState(surfaceStateKey, canReview, initialPanel);
    setPanelWidth(stored.panelWidth);
    setPanel(stored.panel);
  }, [canReview, initialPanel, surfaceStateKey]);

  useEffect(() => {
    if (initialPanel === "review" && canReview) setPanel({ kind: "review" });
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

  return (
    <div className={`agent-session-surface-row is-${context}`} ref={rowRef}>
      <section className={`agent-session-surface is-${context}`}>
        <header className="session-header agent-session-surface-head">
          <span className="session-ticket">{worker.ticket}</span>
          {worker.kind ? <span className="agent-chip">{worker.kind}</span> : null}
          {worker.role ? <span className="agent-chip">{worker.role}</span> : null}
          {worker.model ? <span className="agent-chip is-faint">{worker.model}</span> : null}
          {canReview || onClose ? (
            <div className="agent-surface-actions">
              {canReview ? (
                <button
                  className={`agent-surface-action${panel?.kind === "review" ? " is-active" : ""}`}
                  type="button"
                  onClick={() =>
                    setPanel((current) =>
                      current?.kind === "review" ? null : { kind: "review" }
                    )
                  }
                >
                  <GitPullRequest size={13} />
                  Review
                </button>
              ) : null}
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
          ) : null}
        </header>
        <div className="agent-session-surface-main">
          <SessionTab stateKey={`${surfaceStateKey}:main`} ticket={worker.ticket} onInspect={inspectSubagent} />
        </div>
      </section>
      {panel?.kind === "review" ? (
        <ReviewSidePanel
          canApprove={canReview}
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
