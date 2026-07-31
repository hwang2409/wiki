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
  "claude_limit_hit",
  "claude_limit_cleared",
]);

export function isAgentRefreshEvent(type: string): boolean {
  return AGENT_REFRESH_EVENT_TYPES.has(type);
}
