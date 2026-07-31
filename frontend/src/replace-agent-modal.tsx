import { useEffect, useState, type FormEvent } from "react";
import { RefreshCw, X } from "lucide-react";
import { getAgentModels, replaceAgent } from "./api";
import { isWorkerRole, presetWorkerModel } from "./role-pipeline";
import type {
  AgentModelOption,
  ReplaceAgentResult,
  SpawnWorkerEffort,
  SpawnWorkerKind,
} from "./api";

const REASONING_EFFORTS: SpawnWorkerEffort[] = ["minimal", "low", "medium", "high", "xhigh"];

export type ReplaceAgentTarget = {
  id: string;
  kind: SpawnWorkerKind;
  model: string;
  effort?: SpawnWorkerEffort | null;
  role?: string | null;
};

function defaultModel(
  models: AgentModelOption[],
  kind: SpawnWorkerKind,
  role: string | null | undefined,
) {
  if (isWorkerRole(role)) return presetWorkerModel(models, kind, role);
  const byKind = models.filter((option) => option.kind === kind);
  const field = role === "orchestrator" ? "default_orchestrator" : "default_worker";
  return byKind.find((option) => option[field])?.id ?? byKind[0]?.id ?? "";
}

export function ReplaceAgentModal({
  models,
  onClose,
  onReplaced,
  target,
}: {
  models?: AgentModelOption[];
  onClose: () => void;
  onReplaced?: (result: ReplaceAgentResult) => void;
  target: ReplaceAgentTarget;
}) {
  const [availableModels, setAvailableModels] = useState(models ?? []);
  const [kind, setKind] = useState(target.kind);
  const [model, setModel] = useState(target.model);
  const [effort, setEffort] = useState<SpawnWorkerEffort>(target.effort ?? "high");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (models) {
      setAvailableModels(models);
      return;
    }
    let ignore = false;
    getAgentModels()
      .then((result) => {
        if (!ignore) setAvailableModels(result.models ?? []);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load models");
      });
    return () => {
      ignore = true;
    };
  }, [models]);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !submitting) onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, submitting]);

  const filteredModels = availableModels.filter((option) => option.kind === kind);

  function selectKind(nextKind: SpawnWorkerKind) {
    setKind(nextKind);
    setModel(
      nextKind === target.kind &&
        availableModels.some(
          (option) => option.kind === nextKind && option.id === target.model
        )
        ? target.model
        : defaultModel(availableModels, nextKind, target.role)
    );
    if (nextKind === "cdx") setEffort(target.kind === "cdx" ? target.effort ?? "high" : "high");
    setError(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!model || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const result = await replaceAgent(target.id, {
        kind,
        model,
        ...(kind === "cdx" ? { effort } : {}),
      });
      onReplaced?.(result);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not replace agent");
      setSubmitting(false);
    }
  }

  return (
    <>
      <div className="settings-backdrop" onClick={() => !submitting && onClose()} />
      <form
        aria-label={`Replace ${target.id}`}
        aria-modal
        className="dialog agent-replace-modal"
        role="dialog"
        onSubmit={submit}
      >
        <div className="settings-header">
          <div>
            <div className="dialog-title">Replace {target.id}</div>
            <div className="agent-replace-summary">
              Stop the current run and continue from its durable context.
            </div>
          </div>
          <button
            aria-label="Close replace dialog"
            className="session-close"
            disabled={submitting}
            type="button"
            onClick={onClose}
          >
            <X size={14} />
          </button>
        </div>

        <div className="agent-spawn-fields">
          <div className="agent-spawn-field">
            <span className="agent-spawn-label">Provider kind</span>
            <div aria-label="Provider kind" className="agent-kind-toggle" role="group">
              {(["cc", "cdx"] as const).map((option) => (
                <button
                  aria-pressed={kind === option}
                  className={kind === option ? "is-active" : ""}
                  key={option}
                  type="button"
                  onClick={() => selectKind(option)}
                >
                  <span>{option}</span>
                  <small>{option === "cc" ? "Claude" : "Codex"}</small>
                </button>
              ))}
            </div>
          </div>

          <label className="agent-spawn-field">
            <span className="agent-spawn-label">Model</span>
            <select
              aria-label="Model"
              className="agent-spawn-select"
              disabled={filteredModels.length === 0 || submitting}
              value={model}
              onChange={(event) => setModel(event.target.value)}
            >
              {filteredModels.length === 0 ? <option value="">Loading models</option> : null}
              {filteredModels.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label} · {option.id}
                </option>
              ))}
            </select>
          </label>

          {kind === "cdx" ? (
            <label className="agent-spawn-field">
              <span className="agent-spawn-label">Reasoning effort</span>
              <select
                aria-label="Reasoning effort"
                className="agent-spawn-select"
                disabled={submitting}
                value={effort}
                onChange={(event) => setEffort(event.target.value as SpawnWorkerEffort)}
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

        <div className="agent-spawn-preview">
          <div className="agent-spawn-preview-title">Change</div>
          <div className="agent-spawn-preview-primary">
            <code>
              {target.kind} · {target.model || "unknown"}
              {target.kind === "cdx" && target.effort ? ` · ${target.effort}` : ""}
            </code>
            <span className="agent-replace-arrow" aria-hidden="true">
              {" → "}
            </span>
            <code>
              {kind} · {model || "…"}
              {kind === "cdx" ? ` · ${effort}` : ""}
            </code>
          </div>
        </div>

        <div className="agent-replace-warning">
          This ends the active provider process. The replacement starts with the existing handoff prompt.
        </div>
        {error ? <div className="agent-spawn-error">{error}</div> : null}

        <div className="dialog-actions">
          <button className="dialog-button" disabled={submitting} type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="dialog-button dialog-confirm" disabled={!model || submitting} type="submit">
            <RefreshCw size={12} />
            {submitting ? "Replacing…" : "Replace agent"}
          </button>
        </div>
      </form>
    </>
  );
}
