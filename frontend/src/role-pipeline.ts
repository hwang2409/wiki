import type {
  AgentModelOption,
  SpawnWorkerEffort,
  SpawnWorkerKind,
  SpawnWorkerRole,
} from "./api";

// Default role→model pipeline (vault: tools/orchestrator-worker-protocol.md).
// Falls back to the API's default_worker flag when a preset model is absent.
export const ROLE_PRESETS: Record<
  SpawnWorkerRole,
  { kind: SpawnWorkerKind; model: string; effort: SpawnWorkerEffort }
> = {
  plan: { kind: "cdx", model: "gpt-5.6-luna", effort: "high" },
  implement: { kind: "cdx", model: "gpt-5.6-luna", effort: "high" },
  review: { kind: "cdx", model: "gpt-5.6-sol", effort: "high" },
};

export function modelsForKind(
  models: AgentModelOption[],
  kind: SpawnWorkerKind,
): AgentModelOption[] {
  return models.filter((option) => option.kind === kind);
}

export function defaultWorkerModel(models: AgentModelOption[], kind: SpawnWorkerKind): string {
  const byKind = modelsForKind(models, kind);
  return byKind.find((option) => option.default_worker)?.id ?? byKind[0]?.id ?? "";
}

export function presetWorkerModel(
  models: AgentModelOption[],
  kind: SpawnWorkerKind,
  role: SpawnWorkerRole,
): string {
  const preset = ROLE_PRESETS[role];
  if (
    preset.kind === kind &&
    modelsForKind(models, kind).some((option) => option.id === preset.model)
  ) {
    return preset.model;
  }
  return defaultWorkerModel(models, kind);
}

export function isWorkerRole(role: string | null | undefined): role is SpawnWorkerRole {
  return role === "plan" || role === "implement" || role === "review";
}
