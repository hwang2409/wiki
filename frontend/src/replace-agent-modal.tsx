import { useEffect, useState, type FormEvent } from "react";
import { ChevronDown, RefreshCw } from "lucide-react";
import { getAgentModels, replaceAgent } from "./api";
import { DisclosureContent } from "./disclosure";
import { BbDialog } from "./dialogs";
import type { FocusReturnRef } from "./modal-a11y";
import { Button } from "./primitives";
import { isWorkerRole, presetWorkerModel } from "./role-pipeline";
import type {
  AgentModelOption,
  ReplaceAgentInput,
  ReplaceAgentResult,
  SpawnWorkerEffort,
  SpawnWorkerKind,
} from "./api";

function providerLabel(kind: SpawnWorkerKind | null | undefined): string {
  if (kind === "cc") return "Claude";
  if (kind === "cdx") return "Codex";
  return "unknown";
}

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
  fallbackRef,
}: {
  fallbackRef?: FocusReturnRef;
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
  const [advancedOpen, setAdvancedOpen] = useState(false);

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
      const input: ReplaceAgentInput = {
        kind,
        model,
      };
      if (kind === "cdx") input.effort = effort;
      const result = await replaceAgent(target.id, input);
      onReplaced?.(result);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not replace agent");
      setSubmitting(false);
    }
  }

  return (
    <BbDialog
      as="form"
      title={`Replace ${target.id}`}
      description="Stop the current run and continue from its durable context."
      size="md"
      closeLabel="Close replace dialog"
      busy={submitting}
      fallbackRef={fallbackRef}
      onClose={onClose}
      onSubmit={submit}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={submitting}>
            Cancel
          </Button>
          <Button
            type="submit"
            variant="default"
            disabled={!model || submitting}
            leadingIcon={<RefreshCw size={12} />}
          >
            {submitting ? "Replacing…" : "Replace agent"}
          </Button>
        </>
      }
    >
      <div className="agent-spawn-fields">
          {/* Default view leads with the task and the change preview. Provider /
              model / effort tuning lives inside Advanced. */}
          <div className="agent-spawn-preview">
            <div className="agent-spawn-preview-title">Change</div>
            <div className="agent-spawn-preview-primary">
              <code>
                {providerLabel(target.kind)} · {target.model || "unknown"}
                {target.kind === "cdx" && target.effort ? ` · ${target.effort}` : ""}
              </code>
              <span className="agent-replace-arrow" aria-hidden="true">
                {" → "}
              </span>
              <code>
                {providerLabel(kind)} · {model || "…"}
                {kind === "cdx" ? ` · ${effort}` : ""}
              </code>
            </div>
          </div>

          <div className="agent-spawn-advanced">
            <button
              aria-controls="replace-advanced-body"
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
              <div className="agent-spawn-advanced-body" id="replace-advanced-body">
                <div className="agent-spawn-field">
                  <span className="agent-spawn-label">Provider</span>
                  <div aria-label="Provider" className="agent-kind-toggle" role="group">
                    {(["cc", "cdx"] as const).map((option) => (
                      <button
                        aria-pressed={kind === option}
                        className={kind === option ? "is-active" : ""}
                        key={option}
                        type="button"
                        onClick={() => selectKind(option)}
                      >
                        <span>{providerLabel(option)}</span>
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
                        {option.label}
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
            </DisclosureContent>
          </div>
        </div>

      <div className="agent-replace-warning">
        This ends the active provider process. The replacement starts with the existing handoff prompt.
      </div>
      {error ? <div className="agent-spawn-error">{error}</div> : null}
    </BbDialog>
  );
}
