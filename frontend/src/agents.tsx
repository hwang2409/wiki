import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import {
  AlertTriangle,
  Archive,
  Bot,
  ChevronDown,
  ExternalLink,
  GitPullRequest,
  MoreHorizontal,
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
  previewAgentContextPrelude,
  spawnAgentOrchestrator,
  spawnAgentWorker,
} from "./api";
import type {
  AccountEvent,
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
import { DisclosureContent } from "./disclosure";
import { ROLE_PRESETS, defaultWorkerModel, modelsForKind, presetWorkerModel } from "./role-pipeline";
import { externalLinkProps } from "./external-links";
import { LoadingPlaceholder } from "./loading";
import { ReplaceAgentModal, type ReplaceAgentTarget } from "./replace-agent-modal";
import { ScreencastProvider, ScreencastStrip } from "./screencast-strip";
import { SessionSidebar } from "./session";
import type { SidebarTarget } from "./session";
import { BranchPill } from "./branch-pill";
import { StatusBadge } from "./status-badge";

declare global {
  interface Window {
    __wiki147CoalesceObserved?: {
      runId: string;
      targetSeq: number;
      count: number;
    };
  }
}

const STALE_SECONDS = 5 * 60;

const SPAWN_TICKET_PATTERN = /^[A-Z][A-Z0-9]+-[0-9]+(?:-[A-Z0-9]+)*$/;
const ORCH_ID_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_-]*$/;
export const DEFAULT_WORKDIR = "/Users/henry/me/fun/wiki";
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

// Friendly, task-facing provider names. The internal kind codes ("cc", "cdx")
// stay in the API contract and appear in Technical details, but every
// operator-facing surface uses the product names.
function providerLabel(kind: string | null | undefined): string {
  if (kind === "cc") return "Claude";
  if (kind === "cdx") return "Codex";
  return kind ?? "unknown";
}

// One state-specific primary action per card (WIKI-154 finding 3). Everything
// else (Replace, other lifecycle controls, output preview) lives in the card
// overflow menu. Card-body click always opens the session preview, so an
// implicit "open session" primary is not surfaced as a button.
//
// Interrupt/Revive are TRANSPORT controls: they act on the live provider
// adapter, so eligibility must key off runtime_state (what the adapter is
// actually doing), never the manual worker.state (what the worker last
// claimed in its status file). Confusing the two on Claude leads to hitting
// Interrupt on an idle adapter and marking it interrupted for no reason.
// Review is a TASK control (opens the PR review flow), so it correctly keys
// off worker.state.
type PrimaryActionKind = "archive-dead" | "interrupt" | "resume" | "review" | null;

function primaryActionForWorker(worker: AgentWorker, deadRun: boolean): PrimaryActionKind {
  if (deadRun) return "archive-dead";
  if (worker.pr && worker.state === "merge-ready") return "review";
  if (!worker.run_id) return null;
  const runtime = worker.runtime_state;
  const controlAttached = Boolean(worker.control_attached);
  if (controlAttached && (runtime === "starting" || runtime === "working" || runtime === "waiting-approval")) {
    return "interrupt";
  }
  if (!controlAttached && (runtime === "working" || runtime === "idle" || runtime === "blocked")) {
    return "resume";
  }
  return null;
}

function archivedAge(iso: string): string {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 3600) return `${Math.max(1, Math.floor(seconds / 60))}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const SCREENCAST_EXPANDED_KEY = "wiki-expanded-screencasts";

function readStoredExpandedScreencasts(): Set<string> {
  try {
    const raw = localStorage.getItem(SCREENCAST_EXPANDED_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(
      Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === "string") : []
    );
  } catch {
    return new Set();
  }
}

export function SpawnWorkerModal({
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
  const [title, setTitle] = useState("");
  const [prompt, setPrompt] = useState("");
  const [contextEnabled, setContextEnabled] = useState(false);
  const [prelude, setPrelude] = useState("");
  const [preludeReady, setPreludeReady] = useState(false);
  const [preludeLoading, setPreludeLoading] = useState(false);
  const [preludeError, setPreludeError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  function requestClose() {
    if (submitting) return;
    onClose();
  }

  function resetConfirmation() {
    setConfirming(false);
    setError(null);
  }

  function invalidatePrelude() {
    if (!contextEnabled) return;
    setPreludeReady(false);
    setPreludeLoading(true);
    setPreludeError(null);
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
      allowed.some((option) => option.id === current)
        ? current
        : presetWorkerModel(models, kind, role)
    );
    // role is read for the preset fallback only; role changes apply their
    // preset explicitly in the role <select> handler.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, models]);

  function applyRolePreset(nextRole: SpawnWorkerRole) {
    const preset = ROLE_PRESETS[nextRole];
    setRole(nextRole);
    setKind(preset.kind);
    setModel(presetWorkerModel(models, preset.kind, nextRole));
    setEffort(preset.effort);
  }

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
  const preludeTooLarge = prelude.length > 4096;
  const ticketValid = SPAWN_TICKET_PATTERN.test(normalizedTicket);
  const workdirValid = workdir.trim().length > 0;
  const canSubmit =
    ticketValid &&
    workdirValid &&
    model.length > 0 &&
    prompt.trim().length > 0 &&
    !promptTooLarge &&
    (!contextEnabled ||
      (preludeReady && !preludeLoading && !preludeError && !preludeTooLarge)) &&
    !submitting;

  useEffect(() => {
    if (!contextEnabled) {
      setPrelude("");
      setPreludeReady(false);
      setPreludeLoading(false);
      setPreludeError(null);
      return;
    }
    if (!ticketValid || !workdirValid) {
      setPrelude("");
      setPreludeReady(false);
      setPreludeLoading(false);
      setPreludeError(null);
      return;
    }
    setPreludeReady(false);
    setPreludeLoading(true);
    setPreludeError(null);
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      previewAgentContextPrelude(
        {
          ticket: normalizedTicket,
          title,
          prompt,
          workdir: workdir.trim(),
        },
        controller.signal,
      )
        .then((result) => {
          if (controller.signal.aborted) return;
          setPrelude(result.prelude);
          if (result.prelude.length > 4096) {
            setPreludeReady(false);
            setPreludeError("context preview exceeded the 4096-character limit");
          } else {
            setPreludeReady(true);
          }
          setPreludeLoading(false);
        })
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setPreludeLoading(false);
          setPreludeError(
            reason instanceof Error ? reason.message : "context preview unavailable",
          );
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [contextEnabled, normalizedTicket, ticketValid, title, prompt, workdir, workdirValid]);

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
        title: title.trim(),
        context_prelude: contextEnabled,
        context_prelude_override: contextEnabled ? prelude : null,
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
        aria-labelledby="spawn-worker-dialog-title"
        className="dialog agent-spawn-modal"
        role="dialog"
        onSubmit={submit}
      >
        <div className="settings-header">
          <div className="dialog-title" id="spawn-worker-dialog-title">Spawn worker</div>
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
          {/* Lead: what task, and what the worker should do. */}
          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Ticket</span>
            <input
              required
              className="dialog-input"
              placeholder="WIKI-4"
              value={ticket}
              onChange={(event) => {
                resetConfirmation();
                setTicket(event.target.value.toUpperCase());
                invalidatePrelude();
              }}
            />
            <span className="agent-spawn-hint">Uppercase letters, numbers, and dashes only.</span>
          </label>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Title</span>
            <input
              className="dialog-input"
              placeholder="Short description of the task"
              value={title}
              onChange={(event) => {
                resetConfirmation();
                setTitle(event.target.value);
                invalidatePrelude();
              }}
            />
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
                invalidatePrelude();
              }}
            />
            <span className="agent-spawn-hint">
              {promptTooLarge
                ? "Prompt must stay under 100KB."
                : promptBytes >= 80_000
                ? `${promptBytes} bytes · nearing the 100KB limit`
                : ""}
            </span>
          </label>

          {/* Everything else is provider/model/context tuning. Hide by
              default (WIKI-154 finding 4 — Advanced disclosure). The role
              preset already applied sensible defaults on open. */}
          <div className="agent-spawn-advanced">
            <button
              aria-controls="spawn-advanced-body"
              aria-expanded={advancedOpen}
              className="agent-spawn-advanced-toggle"
              type="button"
              onClick={() => setAdvancedOpen((value) => !value)}
            >
              <ChevronDown
                className={`disclosure-chevron${advancedOpen ? "" : " is-collapsed"}`}
                size={13}
              />
              Advanced
              <span className="agent-spawn-advanced-summary">
                {role} · {providerLabel(kind)} · {model || "default model"}
                {kind === "cdx" ? ` · ${effort}` : ""}
              </span>
            </button>
            <DisclosureContent open={advancedOpen}>
              <div className="agent-spawn-advanced-body" id="spawn-advanced-body">
                <div className="agent-spawn-row">
                  <label className="agent-spawn-field">
                    <span className="agent-spawn-label">Role</span>
                    <select
                      aria-label="Role"
                      className="agent-spawn-select"
                      value={role}
                      onChange={(event) => {
                        resetConfirmation();
                        applyRolePreset(event.target.value as SpawnWorkerRole);
                      }}
                    >
                      <option value="plan">plan</option>
                      <option value="implement">implement</option>
                      <option value="review">review</option>
                    </select>
                    <span className="agent-spawn-hint">
                      Sets the pipeline default provider, model, and effort.
                    </span>
                  </label>

                  <label className="agent-spawn-field">
                    <span className="agent-spawn-label">Provider</span>
                    <select
                      aria-label="Provider"
                      className="agent-spawn-select"
                      value={kind}
                      onChange={(event) => {
                        resetConfirmation();
                        setKind(event.target.value as SpawnWorkerKind);
                      }}
                    >
                      <option value="cdx">Codex</option>
                      <option value="cc">Claude</option>
                    </select>
                  </label>
                </div>

                <div className="agent-spawn-row">
                  <label className="agent-spawn-field">
                    <span className="agent-spawn-label">Model</span>
                    <select
                      aria-label="Model"
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
                        aria-label="Reasoning effort"
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
                  ) : null}
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
                      invalidatePrelude();
                    }}
                  />
                </label>

                <label className="agent-spawn-field">
                  <span className="agent-spawn-label">Orchestrator</span>
                  <select
                    aria-label="Orchestrator"
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
                  <span className="agent-spawn-label">
                    <input
                      type="checkbox"
                      checked={contextEnabled}
                      onChange={(event) => {
                        const enabled = event.target.checked;
                        resetConfirmation();
                        setContextEnabled(enabled);
                        setPreludeReady(false);
                        setPreludeError(null);
                        setPreludeLoading(enabled);
                        if (!enabled) setPrelude("");
                      }}
                    />{" "}
                    Add context prelude
                  </span>
                  <textarea
                    aria-label="Context prelude"
                    className="dialog-input agent-spawn-textarea agent-context-prelude"
                    placeholder="Enter a ticket and working dir to preview local context."
                    value={prelude}
                    disabled={!contextEnabled || preludeLoading}
                    onChange={(event) => {
                      resetConfirmation();
                      setPrelude(event.target.value);
                      setPreludeError(null);
                      setPreludeReady(true);
                    }}
                  />
                  <span className="agent-spawn-hint">
                    {!contextEnabled
                      ? "Optional. Enable to retrieve local context."
                      : preludeLoading
                      ? "Building deterministic local context…"
                      : preludeError ||
                        (preludeTooLarge
                          ? "Context prelude must stay within 4096 characters."
                          : prelude.length > 3400
                          ? `${prelude.length} / 4096 characters · nearing limit`
                          : "editable before send")}
                  </span>
                </label>
              </div>
            </DisclosureContent>
          </div>
        </div>

        <div className="agent-spawn-preview">
          <div className="agent-spawn-preview-title">This will create</div>
          <div className="agent-spawn-preview-primary">
            <code>{normalizedTicket || "…"}</code> · {role} worker on{" "}
            {providerLabel(kind)}
          </div>
          <div className="agent-spawn-preview-line">
            {model || "default model"}
            {kind === "cdx" ? ` · ${effort} reasoning effort` : ""}
          </div>
          <div className="agent-spawn-preview-line">
            {orch ? (
              <>
                under orchestrator <code>{orch}</code>
              </>
            ) : (
              "ungrouped — no orchestrator"
            )}{" "}
            · in <code>{workdir.trim() || "…"}</code>
          </div>
          {contextEnabled ? (
            <div className="agent-spawn-preview-line">
              context prelude enabled ({prelude.length} chars)
            </div>
          ) : null}
        </div>

        {!ticketValid && normalizedTicket ? (
          <div className="agent-spawn-error">Ticket ids must stay uppercase and match the worker pattern.</div>
        ) : null}
        {!workdirValid ? <div className="agent-spawn-error">Working dir is required.</div> : null}
        {preludeError ? <div className="agent-spawn-error">{preludeError}</div> : null}
        {error ? <div className="agent-spawn-error">{error}</div> : null}

        <div className="dialog-actions">
          <button className="dialog-button" type="button" onClick={requestClose}>
            Cancel
          </button>
          <button className="dialog-button dialog-confirm" disabled={!canSubmit} type="submit">
            {submitting ? "Spawning…" : confirming ? "Confirm spawn" : "Spawn"}
          </button>
        </div>
      </form>
    </>
  );
}

export function SpawnOrchestratorModal({
  models,
  workspaceRoot,
  workspaceRootReady = true,
  onClose,
  onSpawn,
}: {
  models: AgentModelOption[];
  workspaceRoot?: string | null;
  workspaceRootReady?: boolean;
  onClose: () => void;
  onSpawn: (notice: OrchestratorSpawnNotice) => void;
}) {
  const [id, setId] = useState("");
  const [projectDir, setProjectDir] = useState(workspaceRoot?.trim() ?? "");
  const [kind, setKind] = useState<SpawnWorkerKind>("cc");
  const [model, setModel] = useState("");
  const [effort, setEffort] = useState<SpawnWorkerEffort>("high");
  const [goal, setGoal] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const projectDirTouched = useRef(false);
  const workspaceRootProvided = workspaceRootReady && Boolean(workspaceRoot?.trim());

  useEffect(() => {
    if (projectDirTouched.current) return;
    setProjectDir(workspaceRoot?.trim() ?? "");
  }, [workspaceRoot]);

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
  const canSubmit = idValid && projectDirValid && model.length > 0 && !goalTooLarge && !submitting;

  const projectDirField = (
    <label className="agent-spawn-field">
      <span className="agent-spawn-label">Project directory</span>
      <input
        required
        className="dialog-input"
        placeholder="/tmp/project"
        value={projectDir}
        onChange={(event) => {
          projectDirTouched.current = true;
          resetConfirmation();
          setProjectDir(event.target.value);
        }}
      />
      {!workspaceRootProvided ? (
        <span className="agent-spawn-hint">Choose the active workspace root before launching.</span>
      ) : null}
    </label>
  );

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
        aria-labelledby="spawn-orchestrator-dialog-title"
        className="dialog agent-spawn-modal"
        role="dialog"
        onSubmit={submit}
      >
        <div className="settings-header">
          <div className="dialog-title" id="spawn-orchestrator-dialog-title">Spawn orchestrator</div>
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
          {/* Lead: who this orchestrator is and what it should do. Provider
              tuning lives inside Advanced (WIKI-154 finding 4 → round-5
              family sweep, orchestrator dialog). */}
          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Name</span>
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
              {goalTooLarge
                ? "Goal must stay under 20KB."
                : goalBytes >= 16_000
                ? `${goalBytes} bytes · nearing the 20KB limit`
                : ""}
            </span>
          </label>

          {!workspaceRootProvided ? projectDirField : null}

          <div className="agent-spawn-advanced">
            <button
              aria-controls="spawn-orch-advanced-body"
              aria-expanded={advancedOpen}
              className="agent-spawn-advanced-toggle"
              type="button"
              onClick={() => setAdvancedOpen((value) => !value)}
            >
              <ChevronDown
                className={`disclosure-chevron${advancedOpen ? "" : " is-collapsed"}`}
                size={13}
              />
              Advanced
              <span className="agent-spawn-advanced-summary">
                {providerLabel(kind)} · {model || "default model"}
                {kind === "cdx" ? ` · ${effort}` : ""}
              </span>
            </button>
            <DisclosureContent open={advancedOpen}>
              <div className="agent-spawn-advanced-body" id="spawn-orch-advanced-body">
                {workspaceRootProvided ? projectDirField : null}

                <label className="agent-spawn-field">
                  <span className="agent-spawn-label">Provider</span>
                  <select
                    aria-label="Provider"
                    className="agent-spawn-select"
                    value={kind}
                    onChange={(event) => {
                      resetConfirmation();
                      setKind(event.target.value as SpawnWorkerKind);
                    }}
                  >
                    <option value="cc">Claude</option>
                    <option value="cdx">Codex</option>
                  </select>
                </label>

                <div className="agent-spawn-row">
                  <label className="agent-spawn-field">
                    <span className="agent-spawn-label">Model</span>
                    <select
                      aria-label="Model"
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
                        aria-label="Reasoning effort"
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
                  ) : null}
                </div>
              </div>
            </DisclosureContent>
          </div>
        </div>

        <div className="agent-spawn-preview">
          <div className="agent-spawn-preview-title">This will create</div>
          <div className="agent-spawn-preview-primary">
            <code>{normalizedId || "…"}</code> · orchestrator on {providerLabel(kind)}
          </div>
          <div className="agent-spawn-preview-line">
            {model || "default model"}
            {kind === "cdx" ? ` · ${effort} reasoning effort` : ""}
          </div>
          <div className="agent-spawn-preview-line">
            in <code>{projectDir.trim() || "…"}</code>
          </div>
          <div className="agent-spawn-preview-line">
            {goal.trim().length > 0
              ? `initial goal ${goalBytes} bytes`
              : "no initial goal — reports ready and waits"}
          </div>
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
            {submitting ? "Launching…" : confirming ? "Confirm launch" : "Launch"}
          </button>
        </div>
      </form>
    </>
  );
}

export type { AccountEvent } from "./api";

function countWorkers(count: number): string {
  return `${count} worker${count === 1 ? "" : "s"}`;
}

// Impact-first copy: what happened to the fleet, then the next action.
function accountBannerCopy(event: AccountEvent): { impact: string; action: string | null } {
  switch (event.type) {
    case "codex_rotation": {
      const from = event.from ? event.from : "(unset)";
      const failed = event.failed.length;
      return {
        impact: `Codex account switched ${from} → ${event.to}; ${countWorkers(event.revived.length)} resumed automatically.`,
        action:
          failed > 0
            ? `${countWorkers(failed)} did not resume — revive or replace them from their cards.`
            : null,
      };
    }
    case "codex_limit_no_eligible":
      return {
        impact: `Codex usage limit reached on every account — ${event.tickets.length > 0 ? `${event.tickets.join(", ")} are` : "Codex workers are"} paused.`,
        action: event.reset_at
          ? `When the limit resets at ${event.reset_at}, revive these workers. To keep moving now, replace them with Claude workers.`
          : "Revive these workers after the limit resets, or replace them with Claude workers.",
      };
    case "codex_rotation_failed":
      return {
        impact: "Codex account rotation failed — paused Codex workers stay paused.",
        action: "Fix Codex auth, then revive workers from their cards.",
      };
    case "codex_auth_dead_revival": {
      const failed = event.failed.length;
      return {
        impact: `Codex sign-in recovered; ${countWorkers(event.revived.length)} restarted.`,
        action:
          failed > 0
            ? `${countWorkers(failed)} did not restart — replace them from their cards.`
            : null,
      };
    }
    case "codex_auth_dead_exhausted":
      return {
        impact: `Codex sign-in is dead and automatic restarts ran out for ${event.tickets.join(", ")}.`,
        action: "Sign in to Codex again, then replace these workers.",
      };
    case "claude_limit_hit":
      // A confirmed successful Claude turn clears this notice for the same
      // run. Replacement remains the continue-now option.
      return {
        impact: `Claude usage limit hit — ${event.ticket} is paused.`,
        action: "Retry after the limit resets; the next successful turn clears this notice. To continue now, replace it with a Codex worker.",
      };
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

function TechDetails({ rows }: { rows: Array<[string, ReactNode] | null | false> }) {
  const visible = rows.filter(Boolean) as Array<[string, ReactNode]>;
  if (visible.length === 0) {
    return <div className="agent-tech-empty">No technical details recorded.</div>;
  }
  return (
    <dl className="agent-tech">
      {visible.map(([term, value]) => (
        <div className="agent-tech-row" key={term}>
          <dt>{term}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

// Raw diagnostics (backend exception text, per-worker failure reasons) stay
// out of the default banner and render only inside the details disclosure.
function bannerDiagnostics(event: AccountEvent): string[] {
  const lines: string[] = [];
  if (event.type === "codex_rotation_failed" && event.error) {
    lines.push(event.error);
  }
  if (event.type === "codex_rotation" || event.type === "codex_auth_dead_revival") {
    const reasons = event.failed_reasons ?? {};
    for (const ticket of event.failed) {
      lines.push(reasons[ticket] ? `${ticket}: ${reasons[ticket]}` : ticket);
    }
  }
  return lines;
}

function AccountEventsBanner({ events }: { events: AccountEvent[] }) {
  const [openDiagnostics, setOpenDiagnostics] = useState<Set<string>>(new Set());
  if (events.length === 0) return null;
  return (
    <div aria-live="polite" className="agents-account-banner">
      {events.map((event, index) => {
        const copy = accountBannerCopy(event);
        const diagnostics = bannerDiagnostics(event);
        const key = `${event.ts}-${event.type}-${index}`;
        const open = openDiagnostics.has(key);
        return (
          <div
            className={`agents-account-banner-row is-${bannerTone(event)}`}
            key={key}
          >
            <AlertTriangle size={13} />
            <span className="agents-account-banner-copy">
              <span className="agents-account-banner-impact">{copy.impact}</span>
              {copy.action ? (
                <span className="agents-account-banner-action">{copy.action}</span>
              ) : null}
              {diagnostics.length > 0 ? (
                <>
                  <button
                    aria-expanded={open}
                    className="agents-account-banner-details-toggle"
                    type="button"
                    onClick={() =>
                      setOpenDiagnostics((prev) => {
                        const next = new Set(prev);
                        if (next.has(key)) next.delete(key);
                        else next.add(key);
                        return next;
                      })
                    }
                  >
                    <ChevronDown
                      className={`disclosure-chevron${open ? "" : " is-collapsed"}`}
                      size={12}
                    />
                    Technical details
                  </button>
                  <DisclosureContent open={open}>
                    <ul className="agents-account-banner-diagnostics">
                      {diagnostics.map((line, lineIndex) => (
                        <li key={lineIndex}>{line}</li>
                      ))}
                    </ul>
                  </DisclosureContent>
                </>
              ) : null}
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function AgentsView({
  data,
  workspaceRoot,
  workspaceRootReady = true,
  onOpenAgent,
  refreshTick,
  openTicket,
  onOpenTicket,
}: {
  data?: {
    workers: AgentWorker[] | null;
    orchestrators: Orchestrator[];
    archived: ArchivedWorker[];
    error: string | null;
    account_notices?: AccountEvent[];
  };
  workspaceRoot?: string | null;
  workspaceRootReady?: boolean;
  onOpenAgent: (ticket: string, panel?: "review") => void;
  refreshTick: number;
  openTicket: string | null;
  onOpenTicket: (ticket: string | null) => void;
}) {
  const [fetchedWorkers, setFetchedWorkers] = useState<AgentWorker[] | null>(null);
  const [fetchedOrchestrators, setFetchedOrchestrators] = useState<Orchestrator[]>([]);
  const [fetchedArchived, setFetchedArchived] = useState<ArchivedWorker[]>([]);
  const [fetchedNotices, setFetchedNotices] = useState<AccountEvent[]>([]);
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
    account_notices?: AccountEvent[];
  } | null>(null);
  const [expandedScreencasts, setExpandedScreencasts] = useState<Set<string>>(
    readStoredExpandedScreencasts
  );
  const [expandedDetails, setExpandedDetails] = useState<Set<string>>(new Set());
  const [openMenuTicket, setOpenMenuTicket] = useState<string | null>(null);
  // Per-archive selection is WIKI-229: the backend session route currently
  // prefers a live run for the same ticket and consults a ticket-only
  // transcript-path cache before the archived_at hint, so promising a
  // specific archive here would be a false affordance. The stable
  // (ticket, archived_at) identifier still rides through SidebarTarget →
  // SessionTab → getAgentSession → /session?archived_at=… so WIKI-229 can
  // switch on it once the route is discriminated; until then history rows
  // open the ticket's transcript view (as they did pre-WIKI-154).

  useEffect(() => {
    if (openMenuTicket === null) return;
    function onDown(event: MouseEvent) {
      const target = event.target as HTMLElement | null;
      if (target && target.closest(`[data-agent-menu-for="${openMenuTicket}"]`)) return;
      setOpenMenuTicket(null);
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpenMenuTicket(null);
    }
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [openMenuTicket]);

  function toggleDetails(id: string) {
    setExpandedDetails((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  useEffect(() => {
    localStorage.setItem(SCREENCAST_EXPANDED_KEY, JSON.stringify([...expandedScreencasts]));
  }, [expandedScreencasts]);

  function toggleScreencast(ticket: string) {
    setExpandedScreencasts((prev) => {
      const next = new Set(prev);
      if (next.has(ticket)) next.delete(ticket);
      else next.add(ticket);
      return next;
    });
  }

  useEffect(() => {
    if (data) return;
    let ignore = false;
    getAgents()
      .then((result) => {
        if (!ignore) {
          setFetchedWorkers(result.workers);
          setFetchedOrchestrators(result.orchestrators ?? []);
          setFetchedArchived(result.archived ?? []);
          setFetchedNotices(result.account_notices ?? []);
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
  const accountNotices =
    overrideData?.account_notices ?? data?.account_notices ?? fetchedNotices;
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
  // Per-archive selection lands with WIKI-229 (the backend route currently
  // wins on any live run and consults a ticket-only transcript cache
  // before archived_at). Until then a history click resolves to the first
  // (newest) archive for the ticket — same behavior as before this PR.
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
        account_notices: result.account_notices ?? [],
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

  function renderHistoryRow(entry: ArchivedWorker) {
    // History rows are a QUIET outcome/date summary (WIKI-154 finding 3 →
    // round-5 family sweep). Ticket + outcome badge + archived age +
    // "View transcript" are the only default surface. Kind/role/model/step
    // and every technical field live behind the details disclosure.
    //
    // Selecting a specific archive for a ticket with multiple entries is
    // WIKI-229. Until the backend route is discriminated, all history rows
    // for the same ticket open the same transcript (the newest, as this
    // PR does), so per-row selection is deliberately not surfaced here.
    const key = `${entry.ticket}-${entry.archived_at}`;
    const isOpen = openTicket === entry.ticket;
    const detailsOpen = expandedDetails.has(key);
    return (
      <article
        className={`agent-card is-archived${isOpen ? " is-selected" : ""}`}
        key={key}
        onClick={(event) => {
          const target = event.target as HTMLElement;
          if (target.closest("button, a, .agent-tech")) return;
          onOpenTicket(isOpen ? null : entry.ticket);
        }}
      >
        <header className="agent-card-header">
          <a
            className="agent-ticket"
            href={`https://linear.app/phoebework/issue/${entry.ticket}`}
            {...externalLinkProps(`https://linear.app/phoebework/issue/${entry.ticket}`)}
          >
            {entry.ticket}
          </a>
          {entry.outcome ? (
            <StatusBadge label={entry.outcome} state={`outcome-${entry.outcome}`} />
          ) : entry.state ? (
            <StatusBadge label={entry.state} state={entry.state} />
          ) : null}
          <span className="agent-age tabular-nums">{archivedAge(entry.archived_at)}</span>
        </header>
        <div className="agent-meta">
          <span className="agent-actions">
            <button
              className="agent-primary-action"
              type="button"
              onClick={() => onOpenTicket(isOpen ? null : entry.ticket)}
            >
              <ScrollText size={13} />
              View transcript
            </button>
            <button
              aria-expanded={detailsOpen}
              className={`agent-log-toggle${detailsOpen ? " is-active" : ""}`}
              type="button"
              onClick={() => toggleDetails(key)}
            >
              <ChevronDown
                className={`disclosure-chevron${detailsOpen ? "" : " is-collapsed"}`}
                size={13}
              />
              details
            </button>
          </span>
        </div>
        <DisclosureContent open={detailsOpen}>
          <TechDetails
            rows={[
              entry.kind ? ["provider", `${providerLabel(entry.kind)} (${entry.kind})`] : null,
              entry.role ? ["role", entry.role] : null,
              entry.model ? ["model", entry.model] : null,
              entry.pr ? ["PR", entry.pr] : null,
              entry.step ? ["last step", entry.step] : null,
              entry.archived_at ? ["archived at", entry.archived_at] : null,
            ]}
          />
        </DisclosureContent>
      </article>
    );
  }

  function renderOrchGroup(orch: Orchestrator, owned: AgentWorker[]) {
    // Orchestrator row applies the same acceptance rules as the worker card
    // (WIKI-154 finding 3 → round-5 family sweep): id + state + age +
    // one state-specific primary action + Technical details disclosure +
    // overflow menu for Replace/lifecycle secondary actions. Raw kind /
    // model / cwd all live under Technical details.
    const deadRun = isDeadRun(orch);
    const headlessRun = Boolean(orch.run_id);
    const menuOpen = openMenuTicket === orch.id;
    const detailsOpen = expandedDetails.has(orch.id);
    const runtime = orch.runtime_state;
    const controlAttached = Boolean(orch.control_attached);
    const canInterrupt =
      headlessRun &&
      controlAttached &&
      (runtime === "starting" || runtime === "working" || runtime === "waiting-approval");
    const canResume =
      headlessRun &&
      !controlAttached &&
      (runtime === "working" || runtime === "idle" || runtime === "blocked");
    const canComplete =
      headlessRun && controlAttached && (runtime === "idle" || runtime === "interrupted");
    const terminal = runtime === "dead" || runtime === "completed";
    const replaceDisabled = !headlessRun;
    const replaceTarget: ReplaceAgentTarget = {
      id: orch.id,
      kind: orch.kind === "cdx" ? "cdx" : "cc",
      model: orch.model ?? "",
      effort: orch.effort,
      role: "orchestrator",
    };
    // Primary: dead → Archive; attached working/starting/waiting → Interrupt;
    // detached working/idle/blocked → Revive; otherwise none (session is
    // implicitly primary via the row/click chain).
    let primary: "archive-dead" | "interrupt" | "resume" | null = null;
    if (deadRun) primary = "archive-dead";
    else if (canInterrupt) primary = "interrupt";
    else if (canResume) primary = "resume";
    const menuItems: Array<{
      key: string;
      label: string;
      disabled?: boolean;
      title?: string;
      run: () => void;
    }> = [];
    if (canInterrupt && primary !== "interrupt") {
      menuItems.push({
        key: "interrupt",
        label: "Interrupt",
        run: () => void requestControl(orch.id, "interrupt", false),
      });
    }
    if (canResume && primary !== "resume") {
      menuItems.push({
        key: "resume",
        label: "Revive",
        run: () => void requestControl(orch.id, "resume", false),
      });
    }
    if (canComplete) {
      const key = `${orch.id}:archive`;
      const confirming = controlConfirm === key;
      menuItems.push({
        key: "complete",
        label: confirming ? "Confirm complete" : "Complete",
        run: () => void requestControl(orch.id, "archive", true),
      });
    }
    if (!terminal) {
      const key = `${orch.id}:stop`;
      const confirming = controlConfirm === key;
      menuItems.push({
        key: "stop",
        label: confirming ? "Confirm stop" : "Stop",
        disabled: !headlessRun,
        title: !headlessRun
          ? "Legacy tmux runs must be migrated before Stop is available"
          : undefined,
        run: () => {
          if (!headlessRun) return;
          void requestControl(orch.id, "stop", true);
        },
      });
    }
    menuItems.push({
      key: "replace",
      label: "Replace",
      disabled: replaceDisabled,
      title: replaceDisabled
        ? headlessRun
          ? "Registered runtime is not live"
          : "Legacy tmux runs must be migrated before Replace is available"
        : "Stop this run and spawn a replacement",
      run: () => {
        if (replaceDisabled) return;
        setReplaceNotice(null);
        setReplaceTarget(replaceTarget);
      },
    });

    function renderOrchPrimary(): ReactNode {
      if (primary === "archive-dead") return <DeadRunAffordance id={orch.id} />;
      if (primary === "interrupt") {
        const pending = controlPending === `${orch.id}:interrupt`;
        return (
          <button
            className="agent-primary-action"
            disabled={Boolean(controlPending)}
            type="button"
            onClick={() => void requestControl(orch.id, "interrupt", false)}
          >
            {pending ? "Interrupting…" : "Interrupt"}
          </button>
        );
      }
      if (primary === "resume") {
        const pending = controlPending === `${orch.id}:resume`;
        return (
          <button
            className="agent-primary-action"
            disabled={Boolean(controlPending)}
            type="button"
            onClick={() => void requestControl(orch.id, "resume", false)}
          >
            {pending ? "Reviving…" : "Revive"}
          </button>
        );
      }
      return null;
    }

    return (
      <div className="agents-orch-group" key={orch.id}>
        <div className="agents-orch-head">
          <Bot size={13} />
          <span className="agents-orch-id">{orch.id}</span>
          {orch.run_id && !deadRun ? (
            <StatusBadge
              label={stateValueLabel(orch.runtime_state)}
              state={orch.runtime_state ?? "unknown"}
            />
          ) : null}
          <span className="agents-orch-actions">
            {renderOrchPrimary()}
            <button
              className={`agent-log-toggle${openTicket === orch.id ? " is-active" : ""}`}
              type="button"
              onClick={() => onOpenTicket(openTicket === orch.id ? null : orch.id)}
            >
              <ScrollText size={13} />
              session
            </button>
            <button
              aria-expanded={detailsOpen}
              className={`agent-log-toggle${detailsOpen ? " is-active" : ""}`}
              type="button"
              onClick={() => toggleDetails(orch.id)}
            >
              <ChevronDown
                className={`disclosure-chevron${detailsOpen ? "" : " is-collapsed"}`}
                size={13}
              />
              details
            </button>
            {menuItems.length > 0 ? (
              <span className="agent-card-menu" data-agent-menu-for={orch.id}>
                <button
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                  aria-label={`More actions for ${orch.id}`}
                  className="agent-log-toggle agent-card-menu-toggle"
                  type="button"
                  onClick={() => setOpenMenuTicket(menuOpen ? null : orch.id)}
                >
                  <MoreHorizontal size={13} />
                </button>
                {menuOpen ? (
                  <div className="agent-card-menu-popover" role="menu">
                    {menuItems.map((item) => {
                      const reasonId = item.disabled ? `agent-menu-${orch.id}-${item.key}-reason` : undefined;
                      return (
                        <span key={item.key}>
                          {item.disabled && item.title ? (
                            <span className="sr-only" id={reasonId}>
                              {item.title}
                            </span>
                          ) : null}
                          <button
                            aria-describedby={reasonId}
                            aria-disabled={item.disabled ? "true" : undefined}
                            className="agent-card-menu-item"
                            key={item.key}
                            role="menuitem"
                            title={item.title}
                            type="button"
                            onClick={() => {
                              if (item.disabled) return;
                              setOpenMenuTicket(null);
                              item.run();
                            }}
                          >
                            {item.label}
                          </button>
                        </span>
                      );
                    })}
                  </div>
                ) : null}
              </span>
            ) : null}
          </span>
        </div>
        <DisclosureContent open={detailsOpen}>
          <TechDetails
            rows={[
              orch.kind ? ["provider", `${providerLabel(orch.kind)} (${orch.kind})`] : null,
              orch.model ? ["model", orch.model] : null,
              orch.effort ? ["effort", orch.effort] : null,
              orch.cwd ? ["project dir", orch.cwd] : null,
              orch.run_id
                ? [
                    "run",
                    `${orch.run_id} · ${orch.runtime_state ?? "unknown"} · control ${orch.control_attached ? "attached" : "detached"}`,
                  ]
                : null,
              orch.window
                ? ["tmux", `${orch.window}${orch.window_alive ? "" : " · window gone"}`]
                : null,
              orch.log ? ["log", orch.log] : null,
            ]}
          />
        </DisclosureContent>
        {archiveErrors[orch.id] ? (
          <div className="agent-inline-error">{archiveErrors[orch.id]}</div>
        ) : null}
        {owned.map(renderWorker)}
        {owned.length === 0 ? (
          <div className="agents-orch-empty">no registered workers</div>
        ) : null}
      </div>
    );
  }

  function renderWorker(worker: AgentWorker) {
    const deadRun = isDeadRun(worker);
    const flag = healthFlag(worker);
    const state = stateLabel(worker);
    const isOpen = openTicket === worker.ticket;
    const previewOpen = expandedScreencasts.has(worker.ticket);
    const detailsOpen = expandedDetails.has(worker.ticket);
    const menuOpen = openMenuTicket === worker.ticket;
    const replaceTargetForCard: ReplaceAgentTarget = {
      id: worker.ticket,
      kind: worker.kind === "cdx" ? "cdx" : "cc",
      model: worker.model ?? "",
      effort: worker.effort,
      role: worker.role,
    };
    const headlessRun = Boolean(worker.run_id);
    const replaceDisabled = !headlessRun;
    // Same runtime_state vs state split as primaryActionForWorker: transport
    // controls key off runtime_state; every other menu entry (Complete /
    // Stop / Replace / Review) keys off whichever field is semantically
    // correct for that action.
    const runtime = worker.runtime_state;
    const controlAttached = Boolean(worker.control_attached);
    const primary = primaryActionForWorker(worker, deadRun);
    const canInterrupt =
      headlessRun &&
      controlAttached &&
      (runtime === "starting" ||
        runtime === "working" ||
        runtime === "waiting-approval");
    const canResume =
      headlessRun &&
      !controlAttached &&
      (runtime === "working" || runtime === "idle" || runtime === "blocked");
    const canComplete =
      headlessRun && controlAttached && (runtime === "idle" || runtime === "interrupted");
    const terminal = runtime === "dead" || runtime === "completed";
    // Menu items = every applicable lifecycle action MINUS whichever action is
    // already surfaced as the card's primary button. Destructive actions
    // (Complete, Stop, Replace) always live in the menu.
    const menuItems: Array<{
      key: string;
      label: string;
      disabled?: boolean;
      title?: string;
      run: () => void;
    }> = [];
    if (canInterrupt && primary !== "interrupt") {
      menuItems.push({
        key: "interrupt",
        label: "Interrupt",
        run: () => void requestControl(worker.ticket, "interrupt", false),
      });
    }
    if (canResume && primary !== "resume") {
      menuItems.push({
        key: "resume",
        label: "Revive",
        run: () => void requestControl(worker.ticket, "resume", false),
      });
    }
    if (canComplete) {
      const key = `${worker.ticket}:archive`;
      const confirming = controlConfirm === key;
      menuItems.push({
        key: "complete",
        label: confirming ? "Confirm complete" : "Complete",
        run: () => void requestControl(worker.ticket, "archive", true),
      });
    }
    if (!terminal) {
      const key = `${worker.ticket}:stop`;
      const confirming = controlConfirm === key;
      menuItems.push({
        key: "stop",
        label: confirming ? "Confirm stop" : "Stop",
        disabled: !headlessRun,
        title: !headlessRun
          ? "Legacy tmux runs must be migrated before Stop is available"
          : undefined,
        run: () => {
          if (!headlessRun) return;
          void requestControl(worker.ticket, "stop", true);
        },
      });
    }
    menuItems.push({
      key: "replace",
      label: "Replace",
      disabled: replaceDisabled,
      title: replaceDisabled
        ? headlessRun
          ? "Registered runtime is not live"
          : "Legacy tmux runs must be migrated before Replace is available"
        : "Stop this run and spawn a replacement",
      run: () => {
        if (replaceDisabled) return;
        setReplaceNotice(null);
        setReplaceTarget(replaceTargetForCard);
      },
    });
    // Only add Review PR to the menu when Review is not already the primary
    // action — otherwise the same action would appear twice.
    if (worker.pr && primary !== "review") {
      menuItems.push({
        key: "review",
        label: "Review PR",
        run: () => onOpenAgent(worker.ticket, "review"),
      });
    }

    function renderPrimary(): ReactNode {
      if (primary === "archive-dead") {
        return <DeadRunAffordance id={worker.ticket} />;
      }
      if (primary === "interrupt") {
        const key = `${worker.ticket}:interrupt`;
        const pending = controlPending === key;
        return (
          <button
            className="agent-primary-action"
            disabled={Boolean(controlPending)}
            type="button"
            onClick={() => void requestControl(worker.ticket, "interrupt", false)}
          >
            {pending ? "Interrupting…" : "Interrupt"}
          </button>
        );
      }
      if (primary === "resume") {
        const key = `${worker.ticket}:resume`;
        const pending = controlPending === key;
        return (
          <button
            className="agent-primary-action"
            disabled={Boolean(controlPending)}
            type="button"
            onClick={() => void requestControl(worker.ticket, "resume", false)}
          >
            {pending ? "Reviving…" : "Revive"}
          </button>
        );
      }
      if (primary === "review") {
        return (
          <button
            className="agent-primary-action"
            type="button"
            onClick={() => onOpenAgent(worker.ticket, "review")}
          >
            <GitPullRequest size={12} />
            Review
          </button>
        );
      }
      return null;
    }

    return (
      <article
        className={`agent-card${isOpen ? " is-selected" : ""}`}
        key={worker.ticket}
        onClick={(event) => {
          const target = event.target as HTMLElement;
          if (
            target.closest(
              "button, a, input, textarea, select, .agent-screencast, .agent-tech, .agent-card-menu"
            )
          )
            return;
          onOpenTicket(isOpen ? null : worker.ticket);
        }}
      >
        <header className="agent-card-header">
          <a
            className="agent-ticket"
            href={`https://linear.app/phoebework/issue/${worker.ticket}`}
            {...externalLinkProps(`https://linear.app/phoebework/issue/${worker.ticket}`)}
          >
            {worker.ticket}
          </a>
          {deadRun ? null : (
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
          <span className="agent-actions">
            {renderPrimary()}
            <button
              className={`agent-log-toggle${isOpen ? " is-active" : ""}`}
              type="button"
              onClick={() => onOpenTicket(isOpen ? null : worker.ticket)}
            >
              <ScrollText size={13} />
              session
            </button>
            {worker.run_id ? (
              <button
                aria-expanded={previewOpen}
                className={`agent-log-toggle agent-screencast-toggle${previewOpen ? " is-active" : ""}`}
                type="button"
                onClick={() => toggleScreencast(worker.ticket)}
              >
                <ChevronDown
                  className={`disclosure-chevron${previewOpen ? "" : " is-collapsed"}`}
                  size={13}
                />
                output
              </button>
            ) : null}
            <button
              aria-expanded={detailsOpen}
              className={`agent-log-toggle${detailsOpen ? " is-active" : ""}`}
              type="button"
              onClick={() => toggleDetails(worker.ticket)}
            >
              <ChevronDown
                className={`disclosure-chevron${detailsOpen ? "" : " is-collapsed"}`}
                size={13}
              />
              details
            </button>
            {menuItems.length > 0 ? (
              <span className="agent-card-menu" data-agent-menu-for={worker.ticket}>
                <button
                  aria-expanded={menuOpen}
                  aria-haspopup="menu"
                  aria-label={`More actions for ${worker.ticket}`}
                  className="agent-log-toggle agent-card-menu-toggle"
                  type="button"
                  onClick={() =>
                    setOpenMenuTicket(menuOpen ? null : worker.ticket)
                  }
                >
                  <MoreHorizontal size={13} />
                </button>
                {menuOpen ? (
                  <div className="agent-card-menu-popover" role="menu">
                    {menuItems.map((item) => {
                      const reasonId = item.disabled ? `agent-menu-${worker.ticket}-${item.key}-reason` : undefined;
                      return (
                        <span key={item.key}>
                          {item.disabled && item.title ? (
                            <span className="sr-only" id={reasonId}>
                              {item.title}
                            </span>
                          ) : null}
                          <button
                            aria-describedby={reasonId}
                            aria-disabled={item.disabled ? "true" : undefined}
                            className="agent-card-menu-item"
                            key={item.key}
                            role="menuitem"
                            title={item.title}
                            type="button"
                            onClick={() => {
                              if (item.disabled) return;
                              setOpenMenuTicket(null);
                              item.run();
                            }}
                          >
                            {item.label}
                          </button>
                        </span>
                      );
                    })}
                  </div>
                ) : null}
              </span>
            ) : null}
          </span>
        </div>

        <DisclosureContent open={detailsOpen}>
          <TechDetails
            rows={[
              worker.kind ? ["provider", `${providerLabel(worker.kind)} (${worker.kind})`] : null,
              worker.role ? ["role", worker.role] : null,
              worker.model ? ["model", worker.model] : null,
              worker.pr ? ["PR", worker.pr] : null,
              worker.worktree
                ? [
                    "branch",
                    worker.worktree.split("/").slice(-1)[0] ?? worker.worktree,
                  ]
                : null,
              worker.run_id
                ? [
                    "run",
                    `${worker.run_id} · ${worker.runtime_state ?? "unknown"} · control ${worker.control_attached ? "attached" : "detached"}`,
                  ]
                : null,
              worker.window
                ? ["tmux", `${worker.window}${worker.window_alive ? "" : " · window gone"}`]
                : null,
              worker.session !== null
                ? [
                    "session",
                    worker.history.length > 0
                      ? `${worker.session} · prev: ${worker.history
                          .map((s) => `${s.role ?? "?"} (${s.kind ?? "?"}, ${s.outcome ?? "?"})`)
                          .join(" → ")}`
                      : `${worker.session}`,
                  ]
                : null,
              worker.worktree ? ["worktree", worker.worktree] : null,
              worker.log ? ["log", worker.log] : null,
            ]}
          />
        </DisclosureContent>

        {worker.run_id ? (
          <DisclosureContent open={previewOpen}>
            <div className="agent-screencast">
              <ScreencastStrip ticket={worker.ticket} runId={worker.run_id} />
            </div>
          </DisclosureContent>
        ) : null}
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
    const activeCount = orchestrators.length + liveWorkers.length;
    body = (
      <>
        {activeCount > 0 ? (
          <div className="agents-section-head is-primary" data-testid="agents-section-active">
            <span className="agents-section-title">Active</span>
            <span className="agents-section-count tabular-nums">{activeCount}</span>
          </div>
        ) : null}
        {grouped.map(({ orch, owned }) => renderOrchGroup(orch, owned))}

        {ungrouped.length > 0 && grouped.length > 0 ? (
          <div className="agents-section-head">unassigned workers</div>
        ) : null}
        {ungrouped.map(renderWorker)}

        {archived.length > 0 ? (
          <>
            <div className="agents-section-head is-primary" data-testid="agents-section-history">
              <Archive size={13} />
              <span className="agents-section-title">History</span>
              <span className="agents-section-count tabular-nums">{archived.length}</span>
            </div>
            {archived.map(renderHistoryRow)}
          </>
        ) : null}
      </>
    );
  }

  return (
    <ScreencastProvider>
    <div className={`agents-layout${openWorker ? " has-sidebar" : ""}`}>
      <div className="agents-view">
        <div className="agents-toolbar">
          <div>
            <div className="agents-toolbar-title">Runs</div>
            <div className="agents-toolbar-meta">
              {orchestrators.length} orchestrator{orchestrators.length === 1 ? "" : "s"} ·{" "}
              {liveWorkers.length} live worker{liveWorkers.length === 1 ? "" : "s"} ·{" "}
              {archived.length} in history
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
        <AccountEventsBanner events={accountNotices} />
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
          workspaceRoot={workspaceRoot}
          workspaceRootReady={workspaceRootReady}
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
    </ScreencastProvider>
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
  // One controller per run kept alive across backoff. `targetSeq` coalesces
  // the HIGHEST seq observed while inflight (bursts collapse to a single
  // additional post); `attempts` counts total requests within this chain and
  // caps at MAX_ATTEMPTS so an SSE burst never widens the retry budget.
  // WIKI-147 R4 H1.
  type ViewedController = { targetSeq: number; attempts: number };
  const viewedInflight = useRef<Map<string, ViewedController>>(new Map());
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

    const existing = viewedInflight.current.get(runId);
    if (existing) {
      // Controller alive — coalesce the target seq monotonically. No new
      // request fires; the in-flight (or scheduled) attempt picks up the
      // highest seq at post-time. Attempt cap stays intact.
      if (observedSeq > existing.targetSeq) existing.targetSeq = observedSeq;
      // WIKI-147 R7 H1: UI-owned signal that the coalesce-during-flight
      // branch actually ran. Test observers wait on this to release the
      // held first POST — proves React committed the bumped targetSeq
      // while the initial controller was still alive, not after a fresh
      // cycle. Behavioral no-op.
      const prior = window.__wiki147CoalesceObserved;
      const priorCount = prior && prior.runId === runId ? prior.count : 0;
      window.__wiki147CoalesceObserved = {
        runId,
        targetSeq: existing.targetSeq,
        count: priorCount + 1,
      };
      return;
    }

    const MAX_ATTEMPTS = 3;
    const backoffMs = (attempt: number) => 500 * 2 ** (attempt - 1);
    const controller: ViewedController = {
      targetSeq: observedSeq,
      attempts: 0,
    };
    viewedInflight.current.set(runId, controller);

    const markFailed = () => {
      viewedInflight.current.delete(runId);
      setViewedFailed((current) => {
        if (current[runId]) return current;
        return { ...current, [runId]: true };
      });
    };

    const runPost = (): void => {
      const state = viewedInflight.current.get(runId);
      if (!state) return;
      state.attempts += 1;
      const seq = state.targetSeq;
      markRunViewed(runId, seq)
        .then((result) => {
          setViewedOverrides((current) => {
            const prior = current[runId] ?? -1;
            const next = Math.max(prior, result.last_viewed_seq, seq);
            if (next <= prior) return current;
            return { ...current, [runId]: next };
          });
          const active = viewedInflight.current.get(runId);
          // Coalesced target advanced while THIS POST was in flight — the
          // just-persisted seq is now stale. Issue a follow-up against the
          // same attempt budget so the max target eventually reaches the
          // server (WIKI-147 R5 H1). Success on the follow-up walks the
          // override forward; budget exhaustion falls through to the failed
          // indicator, matching catch()-path semantics.
          if (active && active.targetSeq > seq) {
            if (active.attempts >= MAX_ATTEMPTS) {
              markFailed();
              return;
            }
            runPost();
            return;
          }
          viewedInflight.current.delete(runId);
        })
        .catch(() => {
          const active = viewedInflight.current.get(runId);
          if (!active) return;
          if (active.attempts < MAX_ATTEMPTS) {
            const delay = backoffMs(active.attempts);
            const timer = window.setTimeout(() => {
              viewedRetryTimers.current.delete(runId);
              runPost();
            }, delay);
            viewedRetryTimers.current.set(runId, timer);
            return;
          }
          markFailed();
        });
    };

    runPost();
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
    return (
      <div className="nav-empty-cta" data-testid="nav-agents-empty">
        <div className="nav-empty-title">No runs yet</div>
        <div className="nav-empty-body">Runs you spawn will appear here.</div>
        <button
          className="nav-empty-primary"
          type="button"
          data-testid="nav-agents-empty-primary"
          onClick={() => {
            window.location.hash = "#/agents";
          }}
        >
          Start a run
        </button>
        <a
          className="nav-empty-secondary"
          href="#/agents"
          data-testid="nav-agents-empty-secondary"
        >
          Open Agents page
        </a>
      </div>
    );
  }

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

  const workerRow = (worker: AgentWorker, owned: boolean) => {
    const unread = hasUnread(worker);
    const failed = worker.run_id ? viewedFailed[worker.run_id] === true : false;
    const stateKey = worker.state ?? "unknown";
    return (
      <button
        className={`nav-agent${owned ? " is-owned" : ""}${activeTicket === worker.ticket ? " is-active" : ""}${unread ? " has-unread" : ""}${failed ? " has-viewed-failure" : ""}`}
        data-state={stateKey}
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
        ) : failed ? (
          <>
            <span
              aria-hidden="true"
              className="nav-agent-unread is-failed"
              data-testid="nav-agent-viewed-failed"
            />
            <span className="sr-only">read state failed to save</span>
          </>
        ) : null}
        <span className="nav-agent-ticket">{worker.ticket}</span>
        <span className="nav-agent-meta" data-state={stateKey}>{stateLabel(worker)}</span>
        {!owned && worker.orch ? (
          <span className="nav-agent-orch-chip" title={`Coordinator: ${worker.orch}`}>
            {worker.orch}
          </span>
        ) : null}
        <span className="nav-agent-age tabular-nums">{ageLabel(worker.status_age_seconds)}</span>
      </button>
    );
  };

  const orchestratorRow = (orch: Orchestrator) => {
    const meta = orch.run_id
      ? orch.runtime_state ?? "orchestrator"
      : orch.window && !orch.window_alive
        ? "window gone"
        : "orchestrator";
    return (
      <button
        className={`nav-agent is-orch${activeTicket === orch.id ? " is-active" : ""}`}
        data-state="orchestrator"
        key={orch.id}
        type="button"
        onClick={() => onOpen(orch.id)}
        {...dragProps(orch.id)}
      >
        <Bot size={12} />
        <span className="nav-agent-ticket">{orch.id}</span>
        <span className="nav-agent-meta" data-state="orchestrator">{meta}</span>
      </button>
    );
  };

  const attentionRank = (worker: AgentWorker): number => {
    switch (worker.state) {
      case "blocked":
        return 0;
      case "merge-ready":
        return 1;
      case "working":
        return 2;
      default:
        return 3;
    }
  };
  const attentionOrder = (list: AgentWorker[]): AgentWorker[] =>
    [...list].sort((a, b) => {
      const rank = attentionRank(a) - attentionRank(b);
      if (rank !== 0) return rank;
      const ageA = a.status_age_seconds ?? Number.MAX_SAFE_INTEGER;
      const ageB = b.status_age_seconds ?? Number.MAX_SAFE_INTEGER;
      return ageA - ageB;
    });
  const orderedOrchestrators = [...orchestrators].sort((a, b) => a.id.localeCompare(b.id));
  const ungrouped = attentionOrder(
    workers.filter(
      (worker) => !worker.orch || !orchestrators.some((orch) => orch.id === worker.orch)
    )
  );
  const hasActive = orderedOrchestrators.length > 0 || workers.length > 0;
  const hasHistory = archived.length > 0;

  return (
    <div className="nav-agents">
      {hasActive ? (
        <div
          className="nav-agents-group-title"
          data-testid="nav-agents-group-active"
        >
          <span>Active</span>
          <span className="nav-agents-group-count tabular-nums">
            {orderedOrchestrators.length + workers.length}
          </span>
        </div>
      ) : null}
      {orderedOrchestrators.map((orch) => (
        <div key={orch.id}>
          {orchestratorRow(orch)}
          {attentionOrder(workers.filter((worker) => worker.orch === orch.id)).map(
            (worker) => workerRow(worker, true)
          )}
        </div>
      ))}
      {ungrouped.map((worker) => workerRow(worker, false))}
      {hasHistory ? (
        <div
          className="nav-agents-group-title"
          data-testid="nav-agents-group-history"
        >
          <span>History</span>
          <span className="nav-agents-group-count tabular-nums">{archived.length}</span>
        </div>
      ) : null}
      {archived.map((entry) => (
        <button
          className={`nav-agent is-archived${activeTicket === entry.ticket ? " is-active" : ""}`}
          data-state="archived"
          key={`${entry.ticket}-${entry.archived_at}`}
          type="button"
          onClick={() => onOpen(entry.ticket)}
        >
          <span className="nav-agent-ticket">{entry.ticket}</span>
          <span className="nav-agent-meta" data-state="archived">{entry.outcome ?? entry.state ?? ""}</span>
          <span className="nav-agent-age tabular-nums">{archivedAge(entry.archived_at)}</span>
        </button>
      ))}
    </div>
  );
}
