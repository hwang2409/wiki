import type { SessionEvent } from "./api";

// Event types that must refetch /api/agents: fleet mutations plus every
// signal the backend notice store sets or resolves notices on. Missing a
// resolve-side event here leaves a stale banner until an unrelated refresh.
export const AGENT_REFRESH_EVENT_TYPES: ReadonlySet<string> = new Set([
  "vault",
  "agents",
  "codex_rotation",
  "codex_limit_no_eligible",
  "codex_rotation_failed",
  "codex_auth_dead_revival",
  "codex_auth_dead_exhausted",
  "codex_auth_verified",
  "codex_limit_cleared",
  "claude_limit_hit",
  "claude_limit_cleared",
]);

// Registry changes include spawn, replace, and archive transitions. These
// events also refresh workspace discovery because orchestrators can add or
// remove workspace roots.
export const AGENT_TOPOLOGY_EVENT_TYPES: ReadonlySet<string> = new Set(["agents"]);

export function isAgentRefreshEvent(type: string): boolean {
  return AGENT_REFRESH_EVENT_TYPES.has(type);
}

export function isAgentTopologyEvent(type: string): boolean {
  return AGENT_TOPOLOGY_EVENT_TYPES.has(type);
}

export type ActivityStateLabel = "working" | "done" | "failed" | "waiting for you";
export type ActivityRunState = "idle" | "working" | "failed" | "waiting-for-you";

const ACTIVE_PROVIDER_STATES = new Set(["starting", "working", "running", "resuming"]);
const WAITING_PROVIDER_STATES = new Set(["waiting", "waiting-approval", "approval", "input"]);
const FAILED_PROVIDER_STATES = new Set(["dead", "failed", "error", "blocked", "crashed"]);
const IDLE_PROVIDER_STATES = new Set(["idle", "completed"]);

type ToolSummaryMapper = (summary: string) => string;

function cleanSummary(summary: string): string {
  return summary.replace(/[\u0000-\u001f\u007f]+/g, " ").replace(/\s+/g, " ").trim().slice(0, 120);
}

function withoutLead(summary: string, pattern: RegExp, fallback: string): string {
  return cleanSummary(summary).replace(pattern, "").trim() || fallback;
}

// This allowlist is the semantic boundary for collapsed model activity.
// Unknown archetypes stay raw in the expanded transcript and use count fallback.
const ACTIVITY_TOOL_SUMMARIES: Readonly<Record<string, ToolSummaryMapper>> = Object.freeze({
  read: (summary) => `reading ${withoutLead(summary, /^(?:read|view image)\s+/i, "files")}`,
  search: (summary) => `searching ${withoutLead(summary, /^(?:rg|grep|ugrep|find|fd|ag|glob|grep|web search:)\s*/i, "the codebase")}`,
  edit: (summary) => `editing ${withoutLead(summary, /^(?:edit|apply patch)\s*/i, "files")}`,
  git: (summary) => {
    const value = cleanSummary(summary).toLowerCase();
    if (/^git\s+diff\b/.test(value)) return "checking the final diff";
    if (/^git\s+status\b/.test(value)) return "checking repository status";
    return "working with git";
  },
  github: () => "checking GitHub",
  validate: (summary) => {
    const value = cleanSummary(summary).toLowerCase();
    if (/\b(?:pytest|vitest|jest|test)\b/.test(value)) return "running tests";
    if (/\b(?:tsc|mypy|\bty\b)\b/.test(value)) return "checking types";
    if (/\b(?:eslint|ruff)\b/.test(value)) return "running lint checks";
    return "running checks";
  },
  wait: () => "waiting on a process",
  status: () => "updating run status",
  plan: () => "updating the plan",
  run: () => "running a command",
  infra: () => "checking infrastructure",
  monitor: () => "monitoring a process",
  ask: () => "asking for input",
  agent: () => "coordinating agent work",
  steer: () => "steering agent work",
  ticket: () => "checking ticket work",
});

export function activityCountsLabel(events: readonly SessionEvent[]): string {
  const toolCount = events.filter((event) => event.kind === "tool").length;
  const thinkingCount = events.filter((event) => event.kind === "thinking").length;
  const parts: string[] = [];
  if (toolCount) parts.push(`${toolCount} tool call${toolCount === 1 ? "" : "s"}`);
  if (thinkingCount) parts.push(`${thinkingCount} thinking`);
  return parts.join(" · ") || "activity";
}

export function activitySemanticSummary(events: readonly SessionEvent[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.kind === "thinking") return "reasoning through the task";
    if (event.kind !== "tool" || !event.tool) continue;
    const mapper = ACTIVITY_TOOL_SUMMARIES[event.tool.archetype];
    return mapper ? mapper(event.tool.summary || "") : null;
  }
  return null;
}

export function activityStateLabel(
  events: readonly SessionEvent[],
  runState: ActivityRunState = "idle",
): ActivityStateLabel {
  const tools = events.filter((event) => event.kind === "tool" && event.tool).map((event) => event.tool!);
  if (runState === "waiting-for-you") return "waiting for you";
  if (runState === "working") return "working";
  if (runState === "failed") return "failed";
  const latestTool = tools.at(-1);
  if (latestTool?.archetype === "ask" && latestTool.output === null) return "waiting for you";
  if (latestTool?.output === null && latestTool.ok === null) return "working";
  if (latestTool?.ok === false) return "failed";
  return "done";
}

export function activityRunStateFromProvider(
  providerState: string | null | undefined,
  pendingRequestCount: number,
  working: boolean,
): ActivityRunState {
  const state = providerState?.trim().toLowerCase() ?? "";
  if (FAILED_PROVIDER_STATES.has(state)) return "failed";
  if (WAITING_PROVIDER_STATES.has(state) || pendingRequestCount > 0) return "waiting-for-you";
  if (ACTIVE_PROVIDER_STATES.has(state)) return "working";
  if (IDLE_PROVIDER_STATES.has(state)) return "idle";
  return working ? "working" : "idle";
}

export function activityElapsedLabel(events: readonly SessionEvent[]): string | null {
  const times = events
    .flatMap((event) => [event.ts, event.tool?.completed_at])
    .map((timestamp) => timestamp ? Date.parse(timestamp) : Number.NaN)
    .filter(Number.isFinite);
  if (times.length < 2) return null;
  const elapsedMs = Math.max(...times) - Math.min(...times);
  if (elapsedMs <= 0) return null;
  if (elapsedMs < 1_000) return "<1s";
  const seconds = Math.round(elapsedMs / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}
