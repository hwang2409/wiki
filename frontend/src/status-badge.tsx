import { forwardRef } from "react";
import type { AriaRole } from "react";

export type StatusBadgeState =
  | "working"
  | "merge-ready"
  | "blocked"
  | "abandoned"
  | "idle"
  | "waiting-approval"
  | "dead"
  | "interrupted"
  | "ok"
  | "warning"
  | "error"
  | "neutral"
  | "faint"
  | "unknown";

const DEFAULT_LABEL: Partial<Record<StatusBadgeState, string>> = {
  working: "working",
  "merge-ready": "merge-ready",
  blocked: "blocked",
  abandoned: "abandoned",
  idle: "idle",
  "waiting-approval": "waiting approval",
  dead: "dead",
  interrupted: "interrupted",
  ok: "ok",
  warning: "warning",
  error: "error",
  unknown: "unknown",
};

const TAG_STATES = new Set<StatusBadgeState>(["neutral", "faint"]);

const FILE_STATUS_TONE: Record<string, StatusBadgeState> = {
  added: "ok",
  modified: "warning",
  removed: "error",
  deleted: "error",
  renamed: "neutral",
};

// Shared status-to-tone lookup for artifact file rows. Consolidates the
// mapping so both the block renderer (`artifact-block.tsx`) and the panel
// renderer (`artifact-detail/file-list.tsx`) never drift.
export function statusToTone(status: string | null | undefined): StatusBadgeState {
  if (!status) return "neutral";
  return FILE_STATUS_TONE[status.toLowerCase()] ?? "neutral";
}

export type StatusBadgeProps = {
  state: StatusBadgeState | (string & {});
  label?: string;
  withDot?: boolean;
  compact?: boolean;
  title?: string;
  className?: string;
  role?: AriaRole;
};

export const StatusBadge = forwardRef<HTMLSpanElement, StatusBadgeProps>(function StatusBadge(
  { state, label, withDot = false, compact = false, title, className, role },
  ref,
) {
  const classes = ["status-badge", `is-${state}`];
  if (withDot) classes.push("has-dot");
  if (compact) classes.push("is-compact");
  if (className) classes.push(className);
  const text = label ?? DEFAULT_LABEL[state as StatusBadgeState] ?? state;
  const resolvedRole = role ?? (TAG_STATES.has(state as StatusBadgeState) ? undefined : "status");
  return (
    <span
      className={classes.join(" ")}
      data-state={state}
      ref={ref}
      role={resolvedRole}
      title={title ?? text}
    >
      {withDot ? <span aria-hidden="true" className="status-badge-dot" /> : null}
      <span className="status-badge-label">{text}</span>
    </span>
  );
});
