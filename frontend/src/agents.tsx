import { useEffect, useState } from "react";
import {
  AlertTriangle,
  Archive,
  Bot,
  ExternalLink,
  GitBranch,
  GitPullRequest,
  ScrollText,
} from "lucide-react";
import { getAgents } from "./api";
import type { AgentWorker, ArchivedWorker, Orchestrator } from "./api";
import { LoadingPlaceholder } from "./loading";
import { SessionSidebar } from "./session";
import type { SidebarTarget } from "./session";

const STALE_SECONDS = 5 * 60;

function ageLabel(seconds: number | null): string {
  if (seconds === null) return "no status";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function stateLabel(worker: AgentWorker): string {
  if (!worker.state) return "unknown";
  if (worker.state === "merge-ready" && worker.role === "plan") return "plan ready";
  return worker.state;
}

function healthFlag(worker: AgentWorker): string | null {
  if (!worker.registered) return "unregistered — status file without registry entry";
  if (worker.window && !worker.window_alive) return "window gone — worker died or wrapped up?";
  if ((worker.status_age_seconds ?? 0) > STALE_SECONDS && worker.state === "working")
    return "status stale >5m";
  if (!worker.state) return "not reporting yet";
  return null;
}

function archivedAge(iso: string): string {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function AgentsView({
  refreshTick,
  openTicket,
  onOpenTicket,
}: {
  refreshTick: number;
  openTicket: string | null;
  onOpenTicket: (ticket: string | null) => void;
}) {
  const [workers, setWorkers] = useState<AgentWorker[] | null>(null);
  const [orchestrators, setOrchestrators] = useState<Orchestrator[]>([]);
  const [archived, setArchived] = useState<ArchivedWorker[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [openPanel, setOpenPanel] = useState<"session" | "review">("session");

  useEffect(() => {
    let ignore = false;
    getAgents()
      .then((result) => {
        if (!ignore) {
          setWorkers(result.workers);
          setOrchestrators(result.orchestrators ?? []);
          setArchived(result.archived ?? []);
          setError(null);
        }
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load agents");
      });
    return () => {
      ignore = true;
    };
  }, [refreshTick]);

  if (error) return <div className="agents-empty">{error}</div>;
  if (workers === null) {
    return (
      <div className="agents-empty">
        <LoadingPlaceholder className="agents-loading" lines={[94, 88, 91, 76]} />
      </div>
    );
  }
  if (workers.length === 0 && archived.length === 0 && orchestrators.length === 0) {
    return (
      <div className="agents-empty">
        No live workers. Orchestrators register spawns via{" "}
        <code>wiki agent register</code>.
      </div>
    );
  }

  const openOrch = orchestrators.find((orch) => orch.id === openTicket);
  const liveWorker = workers.find((worker) => worker.ticket === openTicket) ?? null;
  const archivedWorker = archived.find((entry) => entry.ticket === openTicket) ?? null;
  const openWorker: SidebarTarget | null = liveWorker
    ? {
        ticket: liveWorker.ticket,
        kind: liveWorker.kind,
        role: liveWorker.role,
        model: liveWorker.model,
        pr: liveWorker.pr,
        canReview: Boolean(liveWorker.pr),
      }
    : openOrch
      ? {
          ticket: openOrch.id,
          kind: "cc",
          role: "orchestrator",
          model: null,
          pr: null,
          canReview: false,
        }
      : archivedWorker
        ? {
            ticket: archivedWorker.ticket,
            kind: archivedWorker.kind,
            role: archivedWorker.role,
            model: archivedWorker.model,
            pr: archivedWorker.pr,
            canReview: false,
          }
        : null;

  const grouped = orchestrators.map((orch) => ({
    orch,
    owned: workers.filter((worker) => worker.orch === orch.id),
  }));
  const ungrouped = workers.filter(
    (worker) => !worker.orch || !orchestrators.some((orch) => orch.id === worker.orch)
  );

  function renderWorker(worker: AgentWorker) {
    const flag = healthFlag(worker);
    const state = stateLabel(worker);
    const isOpen = openTicket === worker.ticket;
    return (
            <article className={`agent-card${isOpen ? " is-selected" : ""}`} key={worker.ticket}>
              <header className="agent-card-header">
                <a
                  className="agent-ticket"
                  href={`https://linear.app/phoebework/issue/${worker.ticket}`}
                  rel="noopener noreferrer"
                  target="_blank"
                >
                  {worker.ticket}
                </a>
                {worker.kind ? <span className="agent-chip">{worker.kind}</span> : null}
                {worker.role ? <span className="agent-chip">{worker.role}</span> : null}
                {worker.model ? <span className="agent-chip is-faint">{worker.model}</span> : null}
                <span className={`agent-state is-${worker.state ?? "unknown"}`}>{state}</span>
                <span className="agent-age tabular-nums">{ageLabel(worker.status_age_seconds)}</span>
              </header>

              {worker.step ? <div className="agent-step">{worker.step}</div> : null}
              {worker.blocker ? (
                <div className="agent-blocker">
                  <AlertTriangle size={13} />
                  <span>{worker.blocker}</span>
                </div>
              ) : null}
              {flag ? (
                <div className="agent-flag">
                  <AlertTriangle size={13} />
                  <span>{flag}</span>
                </div>
              ) : null}

              <div className="agent-meta">
                {worker.window ? (
                  <span className={`agent-window${worker.window_alive ? "" : " is-dead"}`}>
                    tmux {worker.window}
                  </span>
                ) : null}
                {worker.session && worker.history.length > 0 ? (
                  <span className="agent-chain">
                    session {worker.session} · prev:{" "}
                    {worker.history
                      .map((s) => `${s.role ?? "?"} (${s.kind ?? "?"}, ${s.outcome ?? "?"})`)
                      .join(" → ")}
                  </span>
                ) : null}
                {worker.worktree ? (
                  <span className="agent-worktree" title={worker.worktree}>
                    <GitBranch size={11} />
                    {worker.worktree.split("/").slice(-1)[0]}
                  </span>
                ) : null}
                {worker.pr ? (
                  <a
                    className="agent-pr"
                    href={worker.pr}
                    rel="noopener noreferrer"
                    target="_blank"
                  >
                    <ExternalLink size={11} />
                    PR
                  </a>
                ) : null}
                {worker.pr ? (
                  <button
                    className="agent-pr"
                    type="button"
                    onClick={() => {
                      setOpenPanel("review");
                      onOpenTicket(worker.ticket);
                    }}
                  >
                    <GitPullRequest size={11} />
                    Review
                  </button>
                ) : null}
                <button
                  className={`agent-log-toggle${isOpen ? " is-active" : ""}`}
                  type="button"
                  onClick={() => {
                    setOpenPanel("session");
                    onOpenTicket(isOpen ? null : worker.ticket);
                  }}
                >
                  <ScrollText size={13} />
                  log
                </button>
              </div>
            </article>
    );
  }

  return (
    <div className={`agents-layout${openWorker ? " has-sidebar" : ""}`}>
      <div className="agents-view">
        {grouped.map(({ orch, owned }) => (
          <div className="agents-orch-group" key={orch.id}>
            <div className="agents-orch-head">
              <Bot size={13} />
              <span className="agents-orch-id">{orch.id}</span>
              {orch.cwd ? (
                <span className="agents-orch-cwd">{orch.cwd.split("/").slice(-1)[0]}</span>
              ) : null}
              {orch.window && !orch.window_alive ? (
                <span className="agents-orch-dead">window gone</span>
              ) : null}
              <button
                className={`agent-log-toggle${openTicket === orch.id ? " is-active" : ""}`}
                type="button"
                onClick={() => {
                  setOpenPanel("session");
                  onOpenTicket(openTicket === orch.id ? null : orch.id);
                }}
              >
                <ScrollText size={13} />
                log
              </button>
            </div>
            {owned.map(renderWorker)}
            {owned.length === 0 ? (
              <div className="agents-orch-empty">no registered workers</div>
            ) : null}
          </div>
        ))}

        {ungrouped.length > 0 && grouped.length > 0 ? (
          <div className="agents-section-head">workers</div>
        ) : null}
        {ungrouped.map(renderWorker)}

        {archived.length > 0 ? (
          <>
            <div className="agents-section-head">
              <Archive size={13} />
              archived
            </div>
            {archived.map((entry) => {
              const key = `${entry.ticket}-${entry.archived_at}`;
              const isOpen = openTicket === entry.ticket;
              return (
                <article
                  className={`agent-card is-archived${isOpen ? " is-selected" : ""}`}
                  key={key}
                >
                  <header className="agent-card-header">
                    <a
                      className="agent-ticket"
                      href={`https://linear.app/phoebework/issue/${entry.ticket}`}
                      rel="noopener noreferrer"
                      target="_blank"
                    >
                      {entry.ticket}
                    </a>
                    {entry.kind ? <span className="agent-chip">{entry.kind}</span> : null}
                    {entry.role ? <span className="agent-chip">{entry.role}</span> : null}
                    {entry.model ? (
                      <span className="agent-chip is-faint">{entry.model}</span>
                    ) : null}
                    {entry.outcome ? (
                      <span className={`agent-state is-outcome-${entry.outcome}`}>
                        {entry.outcome}
                      </span>
                    ) : entry.state ? (
                      <span className={`agent-state is-${entry.state}`}>{entry.state}</span>
                    ) : null}
                    <span className="agent-age tabular-nums">{archivedAge(entry.archived_at)}</span>
                  </header>
                  <div className="agent-meta">
                    {entry.step ? <span className="agent-chain">{entry.step}</span> : null}
                    {entry.pr ? (
                      <a
                        className="agent-pr"
                        href={entry.pr}
                        rel="noopener noreferrer"
                        target="_blank"
                      >
                        <ExternalLink size={11} />
                        PR
                      </a>
                    ) : null}
                    <button
                      className={`agent-log-toggle${isOpen ? " is-active" : ""}`}
                      type="button"
                      onClick={() => {
                        setOpenPanel("session");
                        onOpenTicket(isOpen ? null : entry.ticket);
                      }}
                    >
                      <ScrollText size={13} />
                      log
                    </button>
                  </div>
                </article>
              );
            })}
          </>
        ) : null}
      </div>
      {openWorker ? (
        <SessionSidebar
          initialTab={openPanel}
          worker={openWorker}
          refreshTick={refreshTick}
          onClose={() => onOpenTicket(null)}
        />
      ) : null}
    </div>
  );
}

export function AgentsSidebar({
  refreshTick,
  activeTicket,
  onOpen,
  onDragStart,
  onDragEnd,
}: {
  refreshTick: number;
  activeTicket: string | null;
  onOpen: (ticket: string) => void;
  onDragStart?: (ticket: string) => void;
  onDragEnd?: () => void;
}) {
  const [workers, setWorkers] = useState<AgentWorker[] | null>(null);
  const [orchestrators, setOrchestrators] = useState<Orchestrator[]>([]);
  const [archived, setArchived] = useState<ArchivedWorker[]>([]);

  useEffect(() => {
    let ignore = false;
    getAgents()
      .then((result) => {
        if (!ignore) {
          setWorkers(result.workers);
          setOrchestrators(result.orchestrators ?? []);
          setArchived(result.archived ?? []);
        }
      })
      .catch(() => {
        if (!ignore) setWorkers([]);
      });
    return () => {
      ignore = true;
    };
  }, [refreshTick]);

  if (workers === null) {
    return (
      <div className="nav-empty">
        <LoadingPlaceholder className="nav-loading" lines={[92, 84, 88, 73]} />
      </div>
    );
  }
  if (workers.length === 0 && archived.length === 0 && orchestrators.length === 0) {
    return <div className="nav-empty">No workers</div>;
  }

  const ungrouped = workers.filter(
    (worker) => !worker.orch || !orchestrators.some((orch) => orch.id === worker.orch)
  );
  const dragProps = (ticket: string) => ({
    draggable: true,
    onDragStart: (event: React.DragEvent) => {
      event.dataTransfer.effectAllowed = "copyMove";
      event.dataTransfer.setData("text/plain", `agent://${ticket}`);
      onDragStart?.(ticket);
    },
    onDragEnd: () => onDragEnd?.(),
  });

  const workerRow = (worker: AgentWorker, indent: boolean) => (
    <button
      className={`nav-agent${indent ? " is-owned" : ""}${activeTicket === worker.ticket ? " is-active" : ""}`}
      key={worker.ticket}
      type="button"
      onClick={() => onOpen(worker.ticket)}
      {...dragProps(worker.ticket)}
    >
      <span className={`nav-agent-dot is-${worker.state ?? "unknown"}`} />
      <span className="nav-agent-ticket">{worker.ticket}</span>
      <span className="nav-agent-meta">{stateLabel(worker)}</span>
      <span className="nav-agent-age tabular-nums">{ageLabel(worker.status_age_seconds)}</span>
    </button>
  );

  return (
    <div className="nav-agents">
      {orchestrators.map((orch) => (
        <div key={orch.id}>
          <button
            className={`nav-agent is-orch${activeTicket === orch.id ? " is-active" : ""}`}
            type="button"
            onClick={() => onOpen(orch.id)}
            {...dragProps(orch.id)}
          >
            <Bot size={12} />
            <span className="nav-agent-ticket">{orch.id}</span>
            <span className="nav-agent-meta">
              {orch.window && !orch.window_alive ? "window gone" : "orchestrator"}
            </span>
          </button>
          {workers
            .filter((worker) => worker.orch === orch.id)
            .map((worker) => workerRow(worker, true))}
        </div>
      ))}
      {ungrouped.map((worker) => workerRow(worker, orchestrators.length > 0))}
      {archived.length > 0 ? <div className="nav-agents-divider">archived</div> : null}
      {archived.map((entry) => (
        <button
          className={`nav-agent is-archived${activeTicket === entry.ticket ? " is-active" : ""}`}
          key={`${entry.ticket}-${entry.archived_at}`}
          type="button"
          onClick={() => onOpen(entry.ticket)}
        >
          <span className="nav-agent-dot is-done" />
          <span className="nav-agent-ticket">{entry.ticket}</span>
          <span className="nav-agent-meta">{entry.outcome ?? entry.state ?? ""}</span>
          <span className="nav-agent-age tabular-nums">{archivedAge(entry.archived_at)}</span>
        </button>
      ))}
    </div>
  );
}
