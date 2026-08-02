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
