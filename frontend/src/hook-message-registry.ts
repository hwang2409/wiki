/*
  Whitelist of user-meaningful transcript markers.

  Markers not present here are treated as internal telemetry and rendered as
  a compact "run detail" chip (raw text hidden behind a disclosure) instead
  of promoted to a product message.

  Severity maps to a single unified row style with three tones:
    - info: neutral hairline
    - warn: attention hairline
    - error: solid outline
*/

export type MarkerSeverity = "info" | "warn" | "error";

export type MarkerRule = {
  severity: MarkerSeverity;
  /**
   * Verb-based user-facing prefix. If provided, prepended to the marker
   * text with a middle dot; otherwise the raw text is shown.
   */
  verb?: string;
};

export const MARKER_WHITELIST: Record<string, MarkerRule> = {
  api_error: { severity: "error" },
  compact_boundary: { severity: "info", verb: "conversation compacted" },
  permission_mode: { severity: "info" },
  "permission-mode": { severity: "info" },
  progress: { severity: "info" },
  subagent: { severity: "info" },
  task_started: { severity: "info" },
  task_complete: { severity: "info" },
  local_command: { severity: "info" },
  model_changed: { severity: "info" },
  scheduled_task_fire: { severity: "info", verb: "scheduled task fired" },
};

export function markerRule(marker: string | undefined): MarkerRule | null {
  if (!marker) return null;
  return MARKER_WHITELIST[marker] ?? null;
}

export function isWhitelistedMarker(marker: string | undefined): boolean {
  return markerRule(marker) !== null;
}
