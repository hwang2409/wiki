import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  Archive,
  Bot,
  ExternalLink,
  GitPullRequest,
  Plus,
  RefreshCw,
  ScrollText,
  X,
} from "lucide-react";
import {
  archiveAgent,
  controlAgent,
  getAgents,
  getAgentModels,
  markRunViewed,
  spawnAgentOrchestrator,
  spawnAgentWorker,
} from "./api";
import type {
  AgentModelOption,
  AgentControlAction,
  AgentControlResult,
  AgentWorker,
  ArchivedWorker,
  Orchestrator,
  ReplaceAgentResult,
  SpawnWorkerEffort,
  SpawnWorkerKind,
  SpawnWorkerRole,
} from "./api";
import { externalLinkProps } from "./external-links";
import { LoadingPlaceholder } from "./loading";
import { ReplaceAgentModal, type ReplaceAgentTarget } from "./replace-agent-modal";
import { SessionSidebar } from "./session";
import type { SidebarTarget } from "./session";
import { BranchPill } from "./branch-pill";
import { StatusBadge } from "./status-badge";

const STALE_SECONDS = 5 * 60;

const SPAWN_TICKET_PATTERN = /^[A-Z0-9-]+$/;
const ORCH_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
const DEFAULT_WORKDIR = "/Users/henry/me/fun/wiki";
const REASONING_EFFORTS: SpawnWorkerEffort[] = ["minimal", "low", "medium", "high", "xhigh"];
const DEAD_RUN_COPY = "adapter detached — archive to reset";

type HeadlessAgentState = {
  run_id?: string | null;
  control_attached?: boolean;
  provider_pid?: number | null;
  runtime_state?: string | null;
  state?: string | null;
  state_reason?: string | null;
};

type WorkerSpawnNotice = {
  kind: "worker";
  ticket: string;
  window: string | null;
  runId: string;
  log: string | null;
  promptPath: string | null;
};

type OrchestratorSpawnNotice = {
  kind: "orchestrator";
  id: string;
  window: string | null;
  runId: string;
  log: string | null;
  promptPath: string | null;
  note: string;
};

type SpawnNotice = WorkerSpawnNotice | OrchestratorSpawnNotice;

function modelsForKind(models: AgentModelOption[], kind: SpawnWorkerKind): AgentModelOption[] {
  return models.filter((option) => option.kind === kind);
}

function defaultWorkerModel(models: AgentModelOption[], kind: SpawnWorkerKind): string {
  const byKind = modelsForKind(models, kind);
  return byKind.find((option) => option.default_worker)?.id ?? byKind[0]?.id ?? "";
}

function defaultOrchestratorModel(models: AgentModelOption[], kind: SpawnWorkerKind): string {
  const byKind = modelsForKind(models, kind);
  if (kind === "cc") {
    return byKind.find((option) => option.default_orchestrator)?.id ?? byKind[0]?.id ?? "";
  }
  return byKind[0]?.id ?? "";
}

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

function stateValueLabel(state: string | null | undefined): string {
  return state || "unknown";
}

function isDeadRun(agent: HeadlessAgentState): boolean {
  if (!agent.run_id) return false;
  const state = agent.state ?? agent.runtime_state;
  return (
    state === "completed" ||
    (!agent.control_attached && agent.provider_pid == null) ||
    agent.state_reason === "adapter_lost"
  );
}

function healthFlag(worker: AgentWorker): string | null {
  if (isDeadRun(worker)) return null;
  if (!worker.registered) return "unregistered — status file without registry entry";
  if (worker.run_id && !worker.control_attached) return "supervisor control is not attached";
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

function SpawnWorkerModal({
  models,
  orchestrators,
  onClose,
  onSpawn,
}: {
  models: AgentModelOption[];
  orchestrators: Orchestrator[];
  onClose: () => void;
  onSpawn: (notice: WorkerSpawnNotice) => void;
}) {
  const [ticket, setTicket] = useState("");
  const [kind, setKind] = useState<SpawnWorkerKind>("cdx");
  const [role, setRole] = useState<SpawnWorkerRole>("implement");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<SpawnWorkerEffort>("high");
  const [workdir, setWorkdir] = useState(DEFAULT_WORKDIR);
  const [orch, setOrch] = useState(orchestrators[0]?.id ?? "");
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function requestClose() {
    if (submitting) return;
    onClose();
  }

  function resetConfirmation() {
    setConfirming(false);
    setError(null);
  }

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, submitting]);

  useEffect(() => {
    const allowed = modelsForKind(models, kind);
    setModel((current) =>
      allowed.some((option) => option.id === current) ? current : defaultWorkerModel(models, kind)
    );
  }, [kind, models]);

  useEffect(() => {
    if (orchestrators.length === 0) {
      setOrch("");
      return;
    }
    setOrch((current) =>
      current && orchestrators.some((candidate) => candidate.id === current)
        ? current
        : orchestrators[0].id
    );
  }, [orchestrators]);

  const normalizedTicket = ticket.trim().toUpperCase();
  const allowedModels = modelsForKind(models, kind);
  const promptBytes = new TextEncoder().encode(prompt).length;
  const promptTooLarge = promptBytes >= 100_000;
  const ticketValid = SPAWN_TICKET_PATTERN.test(normalizedTicket);
  const workdirValid = workdir.trim().length > 0;
  const confirmLabel = `spawn ${kind} · ${model} · ${role} in ${workdir.trim()}?`;
  const canSubmit =
    ticketValid &&
    workdirValid &&
    model.length > 0 &&
    prompt.trim().length > 0 &&
    !promptTooLarge &&
    !submitting;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const result = await spawnAgentWorker({
        ticket: normalizedTicket,
        kind,
        role,
        model,
        effort: kind === "cdx" ? effort : null,
        workdir: workdir.trim(),
        orch: orch || null,
        prompt,
      });
      onSpawn({
        kind: "worker",
        ticket: normalizedTicket,
        window: result.window,
        runId: result.run_id,
        log: result.log,
        promptPath: result.prompt_path,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not spawn worker");
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="settings-backdrop" onClick={requestClose} />
      <form
        aria-modal
        className="dialog agent-spawn-modal"
        role="dialog"
        onSubmit={submit}
      >
        <div className="settings-header">
          <div className="dialog-title">Spawn worker</div>
          <button
            aria-label="Close spawn dialog"
            className="session-close"
            type="button"
            onClick={requestClose}
          >
            <X size={14} />
          </button>
        </div>

        <div className="agent-spawn-fields">
          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Ticket id</span>
            <input
              required
              className="dialog-input"
              placeholder="WIKI-4"
              value={ticket}
              onChange={(event) => {
                resetConfirmation();
                setTicket(event.target.value.toUpperCase());
              }}
            />
            <span className="agent-spawn-hint">Uppercase letters, numbers, and dashes only.</span>
          </label>

          <div className="agent-spawn-row">
            <label className="agent-spawn-field">
              <span className="agent-spawn-label">Kind</span>
              <select
                className="agent-spawn-select"
                value={kind}
                onChange={(event) => {
                  resetConfirmation();
                  setKind(event.target.value as SpawnWorkerKind);
                }}
              >
                <option value="cdx">cdx</option>
                <option value="cc">cc</option>
              </select>
            </label>

            <label className="agent-spawn-field">
              <span className="agent-spawn-label">Role</span>
              <select
                className="agent-spawn-select"
                value={role}
                onChange={(event) => {
                  resetConfirmation();
                  setRole(event.target.value as SpawnWorkerRole);
                }}
              >
                <option value="plan">plan</option>
                <option value="implement">implement</option>
                <option value="review">review</option>
              </select>
            </label>
          </div>

          <div className="agent-spawn-row">
            <label className="agent-spawn-field">
              <span className="agent-spawn-label">Model</span>
              <select
                className="agent-spawn-select"
                disabled={allowedModels.length === 0}
                value={model}
                onChange={(event) => {
                  resetConfirmation();
                  setModel(event.target.value);
                }}
              >
                {allowedModels.length === 0 ? (
                  <option value="">No models available</option>
                ) : (
                  allowedModels.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))
                )}
              </select>
            </label>

            {kind === "cdx" ? (
              <label className="agent-spawn-field">
                <span className="agent-spawn-label">Reasoning effort</span>
                <select
                  className="agent-spawn-select"
                  value={effort}
                  onChange={(event) => {
                    resetConfirmation();
                    setEffort(event.target.value as SpawnWorkerEffort);
                  }}
                >
                  {REASONING_EFFORTS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="agent-spawn-field">
                <span className="agent-spawn-label">Reasoning effort</span>
                <div className="agent-spawn-static">Not used for Claude workers.</div>
              </div>
            )}
          </div>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Working dir</span>
            <input
              required
              className="dialog-input"
              value={workdir}
              onChange={(event) => {
                resetConfirmation();
                setWorkdir(event.target.value);
              }}
            />
          </label>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Orchestrator id</span>
            <select
              className="agent-spawn-select"
              value={orch}
              onChange={(event) => {
                resetConfirmation();
                setOrch(event.target.value);
              }}
            >
              {orchestrators.map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.id}
                </option>
              ))}
              <option value="">{orchestrators.length > 0 ? "ungrouped" : "none"}</option>
            </select>
          </label>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Kickoff prompt</span>
            <textarea
              required
              className="dialog-input agent-spawn-textarea"
              placeholder="Tell the worker exactly what to do."
              value={prompt}
              onChange={(event) => {
                resetConfirmation();
                setPrompt(event.target.value);
              }}
            />
            <span className="agent-spawn-hint">
              {promptTooLarge ? "Prompt must stay under 100KB." : `${promptBytes} bytes`}
            </span>
          </label>
        </div>

        {!ticketValid && normalizedTicket ? (
          <div className="agent-spawn-error">Ticket ids must stay uppercase and match the worker pattern.</div>
        ) : null}
        {!workdirValid ? <div className="agent-spawn-error">Working dir is required.</div> : null}
        {error ? <div className="agent-spawn-error">{error}</div> : null}

        <div className="dialog-actions">
          <button className="dialog-button" type="button" onClick={requestClose}>
            Cancel
          </button>
          <button className="dialog-button dialog-confirm" disabled={!canSubmit} type="submit">
            {submitting ? "Spawning…" : confirming ? confirmLabel : "Spawn"}
          </button>
        </div>
      </form>
    </>
  );
}

function SpawnOrchestratorModal({
  models,
  onClose,
  onSpawn,
}: {
  models: AgentModelOption[];
  onClose: () => void;
  onSpawn: (notice: OrchestratorSpawnNotice) => void;
}) {
  const [id, setId] = useState("");
  const [projectDir, setProjectDir] = useState("");
  const [kind, setKind] = useState<SpawnWorkerKind>("cc");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<SpawnWorkerEffort>("high");
  const [goal, setGoal] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function requestClose() {
    if (submitting) return;
    onClose();
  }

  function resetConfirmation() {
    setConfirming(false);
    setError(null);
  }

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, submitting]);

  useEffect(() => {
    const nextDefault = defaultOrchestratorModel(models, kind);
    setModel((current) =>
      modelsForKind(models, kind).some((option) => option.id === current) ? current : nextDefault
    );
  }, [kind, models]);

  const normalizedId = id.trim();
  const allowedModels = modelsForKind(models, kind);
  const goalBytes = new TextEncoder().encode(goal).length;
  const goalTooLarge = goalBytes >= 20_000;
  const idValid = ORCH_ID_PATTERN.test(normalizedId);
  const projectDirValid = projectDir.trim().length > 0;
  const confirmLabel = `launch ${kind} · ${normalizedId} · ${model} in ${projectDir.trim()}?`;
  const canSubmit = idValid && projectDirValid && model.length > 0 && !goalTooLarge && !submitting;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;
    if (!confirming) {
      setConfirming(true);
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const result = await spawnAgentOrchestrator({
        id: normalizedId,
        workdir: projectDir.trim(),
        kind,
        model,
        effort: kind === "cdx" ? effort : null,
        goal,
      });
      onSpawn({
        kind: "orchestrator",
        id: normalizedId,
        window: result.window,
        runId: result.run_id,
        log: result.log,
        promptPath: result.prompt_path,
        note: result.note,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not spawn orchestrator");
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="settings-backdrop" onClick={requestClose} />
      <form
        aria-modal
        className="dialog agent-spawn-modal"
        role="dialog"
        onSubmit={submit}
      >
        <div className="settings-header">
          <div className="dialog-title">Spawn orchestrator</div>
          <button
            aria-label="Close orchestrator dialog"
            className="session-close"
            type="button"
            onClick={requestClose}
          >
            <X size={14} />
          </button>
        </div>

        <div className="agent-spawn-fields">
          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Orchestrator id</span>
            <input
              required
              className="dialog-input"
              placeholder="wiki-dev"
              value={id}
              onChange={(event) => {
                resetConfirmation();
                setId(event.target.value);
              }}
            />
            <span className="agent-spawn-hint">
              Letters, numbers, dashes, and underscores. Must start with a letter or number.
            </span>
          </label>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Project directory</span>
            <input
              required
              className="dialog-input"
              placeholder="/tmp/project"
              value={projectDir}
              onChange={(event) => {
                resetConfirmation();
                setProjectDir(event.target.value);
              }}
            />
          </label>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Kind</span>
            <select
              className="agent-spawn-select"
              value={kind}
              onChange={(event) => {
                resetConfirmation();
                setKind(event.target.value as SpawnWorkerKind);
              }}
            >
              <option value="cc">cc</option>
              <option value="cdx">cdx</option>
            </select>
          </label>

          <div className="agent-spawn-row">
            <label className="agent-spawn-field">
              <span className="agent-spawn-label">Model</span>
              <select
                className="agent-spawn-select"
                disabled={allowedModels.length === 0}
                value={model}
                onChange={(event) => {
                  resetConfirmation();
                  setModel(event.target.value);
                }}
              >
                {allowedModels.length === 0 ? (
                  <option value="">No models available</option>
                ) : (
                  allowedModels.map((option) => (
                    <option key={option.id} value={option.id}>
                      {option.label}
                    </option>
                  ))
                )}
              </select>
            </label>

            {kind === "cdx" ? (
              <label className="agent-spawn-field">
                <span className="agent-spawn-label">Reasoning effort</span>
                <select
                  className="agent-spawn-select"
                  value={effort}
                  onChange={(event) => {
                    resetConfirmation();
                    setEffort(event.target.value as SpawnWorkerEffort);
                  }}
                >
                  {REASONING_EFFORTS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            ) : (
              <div className="agent-spawn-field">
                <span className="agent-spawn-label">Reasoning effort</span>
                <div className="agent-spawn-static">Not used for Claude orchestrators.</div>
              </div>
            )}
          </div>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Initial goal</span>
            <textarea
              className="dialog-input agent-spawn-textarea"
              placeholder="Optional. Leave empty to have the orchestrator report ready and wait."
              value={goal}
              onChange={(event) => {
                resetConfirmation();
                setGoal(event.target.value);
              }}
            />
            <span className="agent-spawn-hint">
              {goalTooLarge ? "Goal must stay under 20KB." : `${goalBytes} bytes`}
            </span>
          </label>
        </div>

        {!idValid && normalizedId ? (
          <div className="agent-spawn-error">
            Orchestrator ids must start with a letter or number and only use letters, numbers, dashes, or underscores.
          </div>
        ) : null}
        {!projectDirValid ? <div className="agent-spawn-error">Project directory is required.</div> : null}
        {error ? <div className="agent-spawn-error">{error}</div> : null}

        <div className="dialog-actions">
          <button className="dialog-button" type="button" onClick={requestClose}>
            Cancel
          </button>
          <button className="dialog-button dialog-confirm" disabled={!canSubmit} type="submit">
            {submitting ? "Launching…" : confirming ? confirmLabel : "Launch"}
          </button>
        </div>
      </form>
    </>
  );
}

export type AccountEvent =
  | {
      type: "codex_rotation";
      from: string | null;
      to: string;
      revived: string[];
      failed: string[];
      failed_reasons?: Record<string, string>;
      ts: string;
    }
  | {
      type: "codex_limit_no_eligible";
      tickets: string[];
      reset_at: string | null;
      ts: string;
    }
  | {
      type: "codex_rotation_failed";
      error: string;
      ts: string;
    }
  | {
      type: "codex_auth_dead_revival";
      revived: string[];
      failed: string[];
      failed_reasons?: Record<string, string>;
      ts: string;
    }
  | {
      type: "codex_auth_dead_exhausted";
      tickets: string[];
      ts: string;
    }
  | {
      type: "claude_limit_hit";
      ticket: string;
      window: string;
      ts: string;
    };

function failedReasonsSuffix(reasons?: Record<string, string>): string {
  if (!reasons) return "";
  const entries = Object.entries(reasons);
  if (entries.length === 0) return "";
  return ` — ${entries.map(([ticket, reason]) => `${ticket}: ${reason}`).join("; ")}`;
}

function accountBannerLine(event: AccountEvent): string {
  switch (event.type) {
    case "codex_rotation": {
      const from = event.from ? event.from : "(unset)";
      const revived = event.revived.length;
      const failed = event.failed.length;
      const tail = failed > 0 ? `, ${failed} failed to revive` : "";
      return `rotated codex account ${from} → ${event.to}, revived ${revived} worker${revived === 1 ? "" : "s"}${tail}${failedReasonsSuffix(event.failed_reasons)}`;
    }
    case "codex_limit_no_eligible":
      return event.reset_at
        ? `codex usage limit hit, no eligible account until ${event.reset_at}`
        : "codex usage limit hit, no eligible account";
    case "codex_rotation_failed":
      return `codex rotation failed: ${event.error}`;
    case "codex_auth_dead_revival": {
      const revived = event.revived.length;
      const failed = event.failed.length;
      const tail = failed > 0 ? `, ${failed} failed` : "";
      return `codex auth-dead: revived ${revived} worker${revived === 1 ? "" : "s"}${tail}${failedReasonsSuffix(event.failed_reasons)}`;
    }
    case "codex_auth_dead_exhausted":
      return `codex auth-dead: revival cap hit on ${event.tickets.join(", ")} — manual attention needed`;
    case "claude_limit_hit":
      return `claude usage limit hit on ${event.ticket}`;
  }
}

function bannerTone(event: AccountEvent): "info" | "warn" | "danger" {
  if (event.type === "codex_rotation") return "info";
  if (event.type === "codex_auth_dead_revival") {
    return event.failed.length > 0 ? "warn" : "info";
  }
  if (event.type === "claude_limit_hit") return "warn";
  return "danger";
}

function AccountEventsBanner({ events }: { events: AccountEvent[] }) {
  if (events.length === 0) return null;
  return (
    <div aria-live="polite" className="agents-account-banner">
      {events.map((event, index) => (
        <div className={`agents-account-banner-row is-${bannerTone(event)}`} key={`${event.ts}-${index}`}>
          <AlertTriangle size={13} />
          <span>{accountBannerLine(event)}</span>
        </div>
      ))}
    </div>
  );
}

export function AgentsView({
  data,
  onOpenAgent,
  refreshTick,
  openTicket,
  onOpenTicket,
  accountEvents = [],
}: {
  data?: {
    workers: AgentWorker[] | null;
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
    error: string | null;
  };
  onOpenAgent: (ticket: string, panel?: "review") => void;
  refreshTick: number;
  openTicket: string | null;
  onOpenTicket: (ticket: string | null) => void;
  accountEvents?: AccountEvent[];
}) {
  const [fetchedWorkers, setFetchedWorkers] = useState<AgentWorker[] | null>(null);
  const [fetchedOrchestrators, setFetchedOrchestrators] = useState<Orchestrator[]>([]);
  const [fetchedArchived, setFetchedArchived] = useState<ArchivedWorker[]>([]);
  const [fetchedError, setFetchedError] = useState<string | null>(null);
  const [availableModels, setAvailableModels] = useState<AgentModelOption[]>([]);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [spawnWorkerOpen, setSpawnWorkerOpen] = useState(false);
  const [spawnOrchestratorOpen, setSpawnOrchestratorOpen] = useState(false);
  const [spawnNotice, setSpawnNotice] = useState<SpawnNotice | null>(null);
  const [replaceTarget, setReplaceTarget] = useState<ReplaceAgentTarget | null>(null);
  const [replaceNotice, setReplaceNotice] = useState<ReplaceAgentResult | null>(null);
  const [controlConfirm, setControlConfirm] = useState<string | null>(null);
  const [controlPending, setControlPending] = useState<string | null>(null);
  const [controlNotice, setControlNotice] = useState<{
    action: AgentControlAction;
    result: AgentControlResult;
  } | null>(null);
  const [controlError, setControlError] = useState<string | null>(null);
  const [archivePending, setArchivePending] = useState<string | null>(null);
  const [archiveErrors, setArchiveErrors] = useState<Record<string, string>>({});
  const [overrideData, setOverrideData] = useState<{
    workers: AgentWorker[] | null;
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
    error: string | null;
  } | null>(null);

  useEffect(() => {
    if (data) return;
    let ignore = false;
    getAgents()
      .then((result) => {
        if (!ignore) {
          setFetchedWorkers(result.workers);
          setFetchedOrchestrators(result.orchestrators ?? []);
          setFetchedArchived(result.archived ?? []);
          setFetchedError(null);
        }
      })
      .catch((err) => {
        if (!ignore) {
          setFetchedError(err instanceof Error ? err.message : "Could not load agents");
        }
      });
    return () => {
      ignore = true;
    };
  }, [data, refreshTick]);

  useEffect(() => {
    let ignore = false;
    getAgentModels()
      .then((result) => {
        if (!ignore) {
          setAvailableModels(result.models ?? []);
          setModelsError(null);
        }
      })
      .catch((err) => {
        if (!ignore) {
          setModelsError(err instanceof Error ? err.message : "Could not load models");
        }
      });
    return () => {
      ignore = true;
    };
  }, []);

  useEffect(() => {
    setOverrideData(null);
  }, [data, refreshTick]);

  const workers = overrideData?.workers ?? data?.workers ?? fetchedWorkers;
  const orchestrators = overrideData?.orchestrators ?? data?.orchestrators ?? fetchedOrchestrators;
  const archived = overrideData?.archived ?? data?.archived ?? fetchedArchived;
  const error = overrideData?.error ?? data?.error ?? fetchedError;

  useEffect(() => {
    const currentIds = new Set([
      ...(workers ?? []).map((worker) => worker.ticket),
      ...orchestrators.map((orch) => orch.id),
    ]);
    setArchiveErrors((previous) =>
      Object.fromEntries(
        Object.entries(previous).filter(([id]) => currentIds.has(id))
      )
    );
    if (archivePending && !currentIds.has(archivePending)) {
      setArchivePending(null);
    }
  }, [archivePending, orchestrators, workers]);

  const openOrch = orchestrators.find((orch) => orch.id === openTicket);
  const liveWorkers = workers ?? [];
  const liveWorker = liveWorkers.find((worker) => worker.ticket === openTicket) ?? null;
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
          kind: openOrch.kind,
          role: "orchestrator",
          model: openOrch.model,
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
            canReview: Boolean(archivedWorker.pr),
          }
        : null;

  const grouped = orchestrators.map((orch) => ({
    orch,
    owned: liveWorkers.filter((worker) => worker.orch === orch.id),
  }));
  const ungrouped = liveWorkers.filter(
    (worker) => !worker.orch || !orchestrators.some((orch) => orch.id === worker.orch)
  );

  async function requestControl(
    id: string,
    action: AgentControlAction,
    confirm: boolean,
  ) {
    if (controlPending) return;
    const key = `${id}:${action}`;
    if (confirm && controlConfirm !== key) {
      setControlConfirm(key);
      setControlNotice(null);
      setControlError(null);
      return;
    }
    setControlPending(key);
    setControlError(null);
    try {
      const result = await controlAgent(id, action);
      setControlNotice({ action, result });
      setReplaceNotice(null);
      setControlConfirm(null);
    } catch (err) {
      setControlError(err instanceof Error ? err.message : `Could not ${action} agent`);
    } finally {
      setControlPending(null);
    }
  }

  async function requestArchive(id: string) {
    if (archivePending) return;
    setArchivePending(id);
    setArchiveErrors((previous) => {
      const next = { ...previous };
      delete next[id];
      return next;
    });
    setControlConfirm(null);
    setControlNotice(null);
    setControlError(null);
    try {
      await archiveAgent(id);
      const result = await getAgents();
      setOverrideData({
        workers: result.workers,
        orchestrators: result.orchestrators ?? [],
        archived: result.archived ?? [],
        error: null,
      });
    } catch (err) {
      setArchiveErrors((previous) => ({
        ...previous,
        [id]: err instanceof Error ? err.message : "Could not archive agent",
      }));
    } finally {
      setArchivePending((current) => (current === id ? null : current));
    }
  }

  function ReplaceButton({
    disabled = false,
    target,
  }: {
    disabled?: boolean;
    target: ReplaceAgentTarget;
  }) {
    return (
      <button
        className="agent-replace-button"
        disabled={disabled}
        title={disabled ? "Registered runtime is not live" : "Stop this run and spawn a replacement"}
        type="button"
        onClick={() => {
          setReplaceNotice(null);
          setReplaceTarget(target);
        }}
      >
        <RefreshCw size={12} />
        Replace
      </button>
    );
  }

  function LifecycleControls({
    id,
    state,
    controlAttached,
  }: {
    id: string;
    state: string | null | undefined;
    controlAttached: boolean;
  }) {
    const terminal = state === "dead" || state === "completed";
    const canInterrupt =
      controlAttached &&
      (state === "starting" || state === "working" || state === "waiting-approval");
    const canResume =
      !controlAttached &&
      (state === "working" || state === "idle" || state === "blocked");
    const canArchive =
      controlAttached && (state === "idle" || state === "interrupted");

    function button(action: AgentControlAction, label: string, confirm = false) {
      const key = `${id}:${action}`;
      const confirming = confirm && controlConfirm === key;
      const pending = controlPending === key;
      return (
        <button
          className={`agent-replace-button${confirming ? " is-confirming" : ""}`}
          disabled={Boolean(controlPending)}
          key={action}
          type="button"
          onClick={() => {
            void requestControl(id, action, confirm);
          }}
        >
          {pending ? `${label}…` : confirming ? `Confirm ${label.toLowerCase()}` : label}
        </button>
      );
    }

    return (
      <>
        {canInterrupt ? button("interrupt", "Interrupt") : null}
        {canResume ? button("resume", "Revive") : null}
        {canArchive ? button("archive", "Complete", true) : null}
        {!terminal ? button("stop", "Stop", true) : null}
      </>
    );
  }

  function DeadRunAffordance({ id }: { id: string }) {
    const pending = archivePending === id;
    return (
      <>
        <StatusBadge label={DEAD_RUN_COPY} state="detached" />
        <button
          className="agent-archive-button"
          disabled={Boolean(archivePending)}
          type="button"
          onClick={() => {
            void requestArchive(id);
          }}
        >
          <Archive size={12} />
          {pending ? "Archiving…" : "Archive"}
        </button>
      </>
    );
  }

  function renderWorker(worker: AgentWorker) {
    const deadRun = isDeadRun(worker);
    const flag = healthFlag(worker);
    const state = stateLabel(worker);
    const isOpen = openTicket === worker.ticket;
    return (
      <article className={`agent-card${isOpen ? " is-selected" : ""}`} key={worker.ticket}>
        <header className="agent-card-header">
          <a
            className="agent-ticket"
            href={`https://linear.app/phoebework/issue/${worker.ticket}`}
            {...externalLinkProps(`https://linear.app/phoebework/issue/${worker.ticket}`)}
          >
            {worker.ticket}
          </a>
          {worker.kind ? <StatusBadge compact label={worker.kind} state="neutral" /> : null}
          {worker.role ? <StatusBadge compact label={worker.role} state="neutral" /> : null}
          {worker.model ? <StatusBadge compact label={worker.model} state="faint" /> : null}
          {deadRun ? (
            <DeadRunAffordance id={worker.ticket} />
          ) : (
            <StatusBadge label={state} state={worker.state ?? "unknown"} />
          )}
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
        {archiveErrors[worker.ticket] ? (
          <div className="agent-inline-error">{archiveErrors[worker.ticket]}</div>
        ) : null}

        <div className="agent-meta">
          {worker.window ? (
            <span className={`agent-window${worker.window_alive ? "" : " is-dead"}`}>
              tmux {worker.window}
            </span>
          ) : null}
          {worker.run_id ? (
            <span className={`agent-window${worker.control_attached ? "" : " is-dead"}`}>
              run {worker.run_id.slice(0, 8)} · {worker.runtime_state ?? "unknown"}
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
            <BranchPill
              branch={worker.worktree.split("/").slice(-1)[0] ?? worker.worktree}
              title={worker.worktree}
            />
          ) : null}
          {worker.pr ? (
            <a className="agent-pr" href={worker.pr} {...externalLinkProps(worker.pr)}>
              <ExternalLink size={11} />
              PR
            </a>
          ) : null}
          {worker.pr ? (
            <button
              className="agent-pr"
              type="button"
              onClick={() => {
                onOpenAgent(worker.ticket, "review");
              }}
            >
              <GitPullRequest size={11} />
              Review
            </button>
          ) : null}
          <span className="agent-actions">
            {worker.run_id && !deadRun ? (
              <LifecycleControls
                id={worker.ticket}
                state={worker.runtime_state ?? worker.state}
                controlAttached={Boolean(worker.control_attached)}
              />
            ) : null}
            <ReplaceButton
              target={{
                id: worker.ticket,
                kind: worker.kind === "cdx" ? "cdx" : "cc",
                model: worker.model ?? "",
                effort: worker.effort,
                role: worker.role,
              }}
              disabled={!worker.run_id && (!worker.window || !worker.window_alive)}
            />
            <button
              className={`agent-log-toggle${isOpen ? " is-active" : ""}`}
              type="button"
              onClick={() => onOpenTicket(isOpen ? null : worker.ticket)}
            >
              <ScrollText size={13} />
              log
            </button>
          </span>
        </div>
      </article>
    );
  }

  let body: ReactNode;
  if (error) {
    body = <div className="agents-empty">{error}</div>;
  } else if (workers === null) {
    body = (
      <div className="agents-empty">
        <LoadingPlaceholder className="agents-loading" lines={[94, 88, 91, 76]} />
      </div>
    );
  } else if (liveWorkers.length === 0 && archived.length === 0 && orchestrators.length === 0) {
    body = (
      <div className="agents-empty">
        No live workers yet. Spawn one here or register from the terminal with{" "}
        <code>wiki agent register</code>.
      </div>
    );
  } else {
    body = (
      <>
        {grouped.map(({ orch, owned }) => {
          const deadRun = isDeadRun(orch);
          return (
            <div className="agents-orch-group" key={orch.id}>
              <div className="agents-orch-head">
                <Bot size={13} />
                <span className="agents-orch-id">{orch.id}</span>
                {orch.kind ? <StatusBadge compact label={orch.kind} state="neutral" /> : null}
                {orch.model ? <StatusBadge compact label={orch.model} state="faint" /> : null}
                {orch.cwd ? (
                  <span className="agents-orch-cwd">{orch.cwd.split("/").slice(-1)[0]}</span>
                ) : null}
                {orch.run_id ? (
                  deadRun ? (
                    <DeadRunAffordance id={orch.id} />
                  ) : (
                    <StatusBadge
                      label={stateValueLabel(orch.runtime_state)}
                      state={orch.runtime_state ?? "unknown"}
                    />
                  )
                ) : null}
                {orch.run_id ? (
                  <span className={`agent-window${orch.control_attached ? "" : " is-dead"}`}>
                    run {orch.run_id.slice(0, 8)} · {orch.runtime_state ?? "unknown"}
                  </span>
                ) : null}
                {orch.window && !orch.window_alive ? (
                  <span className="agents-orch-dead">window gone</span>
                ) : null}
                <span className="agents-orch-actions">
                  {orch.run_id && !deadRun ? (
                    <LifecycleControls
                      id={orch.id}
                      state={orch.runtime_state}
                      controlAttached={Boolean(orch.control_attached)}
                    />
                  ) : null}
                  <ReplaceButton
                    target={{
                      id: orch.id,
                      kind: orch.kind ?? "cc",
                      model: orch.model ?? "",
                      effort: orch.effort,
                      role: "orchestrator",
                    }}
                    disabled={!orch.run_id && (!orch.window || !orch.window_alive)}
                  />
                  <button
                    className={`agent-log-toggle${openTicket === orch.id ? " is-active" : ""}`}
                    type="button"
                    onClick={() => onOpenTicket(openTicket === orch.id ? null : orch.id)}
                  >
                    <ScrollText size={13} />
                    log
                  </button>
                </span>
              </div>
              {archiveErrors[orch.id] ? (
                <div className="agent-inline-error">{archiveErrors[orch.id]}</div>
              ) : null}
            {owned.map(renderWorker)}
            {owned.length === 0 ? (
              <div className="agents-orch-empty">no registered workers</div>
            ) : null}
            </div>
          );
        })}

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
                      {...externalLinkProps(`https://linear.app/phoebework/issue/${entry.ticket}`)}
                    >
                      {entry.ticket}
                    </a>
                    {entry.kind ? <StatusBadge compact label={entry.kind} state="neutral" /> : null}
                    {entry.role ? <StatusBadge compact label={entry.role} state="neutral" /> : null}
                    {entry.model ? <StatusBadge compact label={entry.model} state="faint" /> : null}
                    {entry.outcome ? (
                      <StatusBadge label={entry.outcome} state={`outcome-${entry.outcome}`} />
                    ) : entry.state ? (
                      <StatusBadge label={entry.state} state={entry.state} />
                    ) : null}
                    <span className="agent-age tabular-nums">{archivedAge(entry.archived_at)}</span>
                  </header>
                  <div className="agent-meta">
                    {entry.step ? <span className="agent-chain">{entry.step}</span> : null}
                    {entry.pr ? (
                      <a
                        className="agent-pr"
                        href={entry.pr}
                        {...externalLinkProps(entry.pr)}
                      >
                        <ExternalLink size={11} />
                        PR
                      </a>
                    ) : null}
                    <button
                      className={`agent-log-toggle${isOpen ? " is-active" : ""}`}
                      type="button"
                      onClick={() => onOpenTicket(isOpen ? null : entry.ticket)}
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
      </>
    );
  }

  return (
    <div className={`agents-layout${openWorker ? " has-sidebar" : ""}`}>
      <div className="agents-view">
        <div className="agents-toolbar">
          <div>
            <div className="agents-toolbar-title">Workers</div>
            <div className="agents-toolbar-meta">
              {orchestrators.length} orchestrator{orchestrators.length === 1 ? "" : "s"} ·{" "}
              {liveWorkers.length} live worker{liveWorkers.length === 1 ? "" : "s"}
            </div>
          </div>
          <div className="dialog-actions">
            <button
              className="agents-spawn-button"
              disabled={workers === null}
              type="button"
              onClick={() => {
                setSpawnNotice(null);
                setSpawnOrchestratorOpen(true);
              }}
            >
              <Bot size={14} />
              Spawn orchestrator
            </button>
            <button
              className="agents-spawn-button"
              disabled={workers === null}
              type="button"
              onClick={() => {
                setSpawnNotice(null);
                setSpawnWorkerOpen(true);
              }}
            >
              <Plus size={14} />
              Spawn worker
            </button>
          </div>
        </div>
        <AccountEventsBanner events={accountEvents} />
        {spawnNotice ? (
          spawnNotice.kind === "worker" ? (
            <div className="agents-notice">
              spawned <code>{spawnNotice.ticket}</code> as run{" "}
              <code>{spawnNotice.runId.slice(0, 8)}</code>
              {spawnNotice.log ? (
                <>
                  {" "}· log <code>{spawnNotice.log}</code>
                </>
              ) : null}
            </div>
          ) : (
            <div className="agents-notice">
              {spawnNotice.note} · run <code>{spawnNotice.runId.slice(0, 8)}</code>
              {spawnNotice.log ? (
                <>
                  {" "}· log <code>{spawnNotice.log}</code>
                </>
              ) : null}
            </div>
          )
        ) : null}
        {replaceNotice ? (
          <div className="agents-notice">
            replaced <code>{replaceNotice.id}</code>
            {replaceNotice.run_id ? (
              <>
                {" "}· run <code>{replaceNotice.run_id.slice(0, 8)}</code>
              </>
            ) : null}
            {replaceNotice.window ? (
              <>
                {" "}· tmux <code>{replaceNotice.window}</code>
              </>
            ) : null}
            {replaceNotice.log ? (
              <>
                {" "}· log <code>{replaceNotice.log}</code>
              </>
            ) : null}
          </div>
        ) : null}
        {controlNotice ? (
          <div className="agents-notice">
            {controlNotice.action} <code>{controlNotice.result.agent_id}</code> · state{" "}
            <code>{controlNotice.result.state}</code>
          </div>
        ) : null}
        {controlError ? <div className="agents-notice is-error">{controlError}</div> : null}
        {modelsError ? <div className="agents-notice is-error">{modelsError}</div> : null}
        {body}
      </div>
      {openWorker ? (
        <SessionSidebar
          onOpenAgent={onOpenAgent}
          worker={openWorker}
          onClose={() => onOpenTicket(null)}
        />
      ) : null}
      {spawnWorkerOpen ? (
        <SpawnWorkerModal
          models={availableModels}
          orchestrators={orchestrators}
          onClose={() => setSpawnWorkerOpen(false)}
          onSpawn={(notice) => {
            setSpawnNotice(notice);
            setSpawnWorkerOpen(false);
            onOpenTicket(notice.ticket);
          }}
        />
      ) : null}
      {spawnOrchestratorOpen ? (
        <SpawnOrchestratorModal
          models={availableModels}
          onClose={() => setSpawnOrchestratorOpen(false)}
          onSpawn={(notice) => {
            setSpawnNotice(notice);
            setSpawnOrchestratorOpen(false);
          }}
        />
      ) : null}
      {replaceTarget ? (
        <ReplaceAgentModal
          models={availableModels}
          target={replaceTarget}
          onClose={() => setReplaceTarget(null)}
          onReplaced={(result) => {
            setReplaceNotice(result);
            setSpawnNotice(null);
            onOpenTicket(result.id);
          }}
        />
      ) : null}
    </div>
  );
}

export function AgentsSidebar({
  data,
  refreshTick,
  activeTicket,
  onOpen,
  onDragStart,
  onDragEnd,
}: {
  data?: {
    workers: AgentWorker[] | null;
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
    error: string | null;
  };
  refreshTick: number;
  activeTicket: string | null;
  onOpen: (ticket: string) => void;
  onDragStart?: (ticket: string) => void;
  onDragEnd?: () => void;
}) {
  const [fetchedWorkers, setFetchedWorkers] = useState<AgentWorker[] | null>(null);
  const [fetchedOrchestrators, setFetchedOrchestrators] = useState<Orchestrator[]>([]);
  const [fetchedArchived, setFetchedArchived] = useState<ArchivedWorker[]>([]);
  // Keyed by run_id (durable). Value is the observed seq we tried to mark
  // viewed at — comparing seqs (monotonic int) sidesteps timestamp-format
  // and wall-clock issues from the round 1 implementation.
  const [viewedOverrides, setViewedOverrides] = useState<Record<string, number>>({});
  // Runs whose mark-viewed request permanently failed (all retries exhausted).
  // Bounded to prevent the round 2 H1 request loop: on failure we keep the
  // optimistic override intact AND set this flag so the effect stops firing;
  // the row renders a distinct failed indicator so the user can see why.
  const [viewedFailed, setViewedFailed] = useState<Record<string, boolean>>({});
  const viewedInflight = useRef<Map<string, boolean>>(new Map());
  const viewedAttempts = useRef<Map<string, number>>(new Map());
  const viewedRetryTimers = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    if (data) return;
    let ignore = false;
    getAgents()
      .then((result) => {
        if (!ignore) {
          setFetchedWorkers(result.workers);
          setFetchedOrchestrators(result.orchestrators ?? []);
          setFetchedArchived(result.archived ?? []);
        }
      })
      .catch(() => {
        if (!ignore) setFetchedWorkers([]);
      });
    return () => {
      ignore = true;
    };
  }, [data, refreshTick]);

  const workers = data?.workers ?? fetchedWorkers;
  const orchestrators = data?.orchestrators ?? fetchedOrchestrators;
  const archived = data?.archived ?? fetchedArchived;

  useEffect(() => {
    if (!activeTicket || workers === null) return;
    const worker = workers.find((row) => row.ticket === activeTicket);
    const runId = worker?.run_id ?? null;
    const observedSeq = worker?.latest_event_seq ?? null;
    if (!runId || observedSeq === null) return;

    // Round 2 H1: once a run's mark-viewed has permanently failed, do not
    // keep firing new requests each render. The optimistic override remains
    // (so the row still visually reflects the user's action) and the failed
    // badge tells them the server didn't persist the state.
    if (viewedFailed[runId]) return;

    // Skip if we (or the server) have already recorded a viewed seq that
    // covers everything visible in this refresh. Prevents the effect from
    // POSTing on every /api/agents refresh (the round 1 regression), while
    // still re-POSTing when the active session's seq advances.
    const priorOverride = viewedOverrides[runId];
    const priorServer = worker?.last_viewed_seq ?? null;
    const priorSeq = Math.max(priorOverride ?? -1, priorServer ?? -1);
    if (priorSeq >= observedSeq) return;

    setViewedOverrides((current) => {
      const prior = current[runId];
      if (prior !== undefined && prior >= observedSeq) return current;
      return { ...current, [runId]: observedSeq };
    });

    if (viewedInflight.current.has(runId)) {
      // Do NOT drop this open — remember that a re-mark is desired once the
      // in-flight request settles. Prevents a stuck "cleared" state if the
      // seq advanced while we were mid-flight.
      viewedInflight.current.set(runId, true);
      return;
    }

    const MAX_ATTEMPTS = 3;
    const backoffMs = (attempt: number) => 500 * 2 ** (attempt - 1);

    const post = (seq: number): void => {
      viewedInflight.current.set(runId, false);
      viewedAttempts.current.set(
        runId,
        (viewedAttempts.current.get(runId) ?? 0) + 1
      );
      markRunViewed(runId, seq)
        .then((result) => {
          viewedAttempts.current.delete(runId);
          setViewedOverrides((current) => ({
            ...current,
            [runId]: result.last_viewed_seq,
          }));
        })
        .catch(() => {
          // Round 2 H1: preserve the optimistic override on failure. Retry
          // with exponential backoff up to MAX_ATTEMPTS; flip to failed
          // afterwards so the effect stops firing and the user sees the
          // failed badge.
          const attempts = viewedAttempts.current.get(runId) ?? MAX_ATTEMPTS;
          if (attempts < MAX_ATTEMPTS) {
            const delay = backoffMs(attempts);
            const timer = window.setTimeout(() => {
              viewedRetryTimers.current.delete(runId);
              post(seq);
            }, delay);
            viewedRetryTimers.current.set(runId, timer);
            return;
          }
          viewedAttempts.current.delete(runId);
          setViewedFailed((current) => {
            if (current[runId]) return current;
            return { ...current, [runId]: true };
          });
        })
        .finally(() => {
          const wasQueued = viewedInflight.current.get(runId) === true;
          viewedInflight.current.delete(runId);
          if (!wasQueued) return;
          // A later open happened while inflight — re-post with the current
          // seq observed on the freshest render.
          setViewedOverrides((current) => {
            const seqNow = current[runId];
            if (seqNow !== undefined && !viewedFailed[runId]) post(seqNow);
            return current;
          });
        });
    };

    post(observedSeq);
  }, [activeTicket, workers, viewedOverrides, viewedFailed]);

  useEffect(() => {
    const timers = viewedRetryTimers.current;
    return () => {
      for (const handle of timers.values()) window.clearTimeout(handle);
      timers.clear();
    };
  }, []);

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

  const hasUnread = (worker: AgentWorker): boolean => {
    const latest = worker.latest_event_seq;
    if (latest === null || latest === undefined) return false;
    const override = worker.run_id ? viewedOverrides[worker.run_id] : undefined;
    const serverSeq = worker.last_viewed_seq;
    const viewed = Math.max(override ?? -1, serverSeq ?? -1);
    if (viewed < 0) return true;
    return latest > viewed;
  };

  const workerRow = (worker: AgentWorker, indent: boolean) => {
    const unread = hasUnread(worker);
    const failed = worker.run_id ? viewedFailed[worker.run_id] === true : false;
    return (
      <button
        className={`nav-agent${indent ? " is-owned" : ""}${activeTicket === worker.ticket ? " is-active" : ""}${unread ? " has-unread" : ""}${failed ? " has-viewed-failure" : ""}`}
        key={worker.ticket}
        type="button"
        onClick={() => onOpen(worker.ticket)}
        title={failed ? "Failed to persist read state to the server" : undefined}
        {...dragProps(worker.ticket)}
      >
        {unread ? (
          <>
            <span aria-hidden="true" className="nav-agent-unread" data-testid="nav-agent-unread" />
            <span className="sr-only">unread</span>
          </>
        ) : null}
        {failed ? (
          <>
            <span
              aria-hidden="true"
              className="nav-agent-unread is-failed"
              data-testid="nav-agent-viewed-failed"
            />
            <span className="sr-only">read state failed to save</span>
          </>
        ) : null}
        <span className={`nav-agent-dot is-${worker.state ?? "unknown"}`} />
        <span className="nav-agent-ticket">{worker.ticket}</span>
        <span className="nav-agent-meta">{stateLabel(worker)}</span>
        <span className="nav-agent-age tabular-nums">{ageLabel(worker.status_age_seconds)}</span>
      </button>
    );
  };

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
              {orch.run_id
                ? orch.runtime_state ?? "unknown"
                : orch.window && !orch.window_alive
                  ? "window gone"
                  : "orchestrator"}
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
