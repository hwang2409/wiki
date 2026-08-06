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

export type ActivityStateLabel = "working" | "done" | "failed" | "interrupted" | "waiting for you";
export type ActivityRunState = "idle" | "working" | "failed" | "interrupted" | "waiting-for-you";

const ACTIVE_PROVIDER_STATES = new Set(["starting", "working", "running", "resuming"]);
const WAITING_PROVIDER_STATES = new Set(["waiting", "waiting-approval", "approval", "input"]);
const FAILED_PROVIDER_STATES = new Set(["dead", "failed", "error", "blocked", "crashed"]);
const INTERRUPTED_PROVIDER_STATES = new Set(["interrupted"]);
const IDLE_PROVIDER_STATES = new Set(["idle", "completed"]);

export function activityStateLabel(
  events: readonly SessionEvent[],
  runState: ActivityRunState = "idle",
): ActivityStateLabel {
  const tools = events.filter((event) => event.kind === "tool" && event.tool).map((event) => event.tool!);
  if (runState === "waiting-for-you") return "waiting for you";
  if (runState === "working") return "working";
  if (runState === "failed") return "failed";
  if (runState === "interrupted") return "interrupted";
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
  if (INTERRUPTED_PROVIDER_STATES.has(state)) return "interrupted";
  if (WAITING_PROVIDER_STATES.has(state) || pendingRequestCount > 0) return "waiting-for-you";
  if (ACTIVE_PROVIDER_STATES.has(state)) return "working";
  if (IDLE_PROVIDER_STATES.has(state)) return "idle";
  return working ? "working" : "idle";
}
