import { forwardRef } from "react";

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

export type StatusBadgeProps = {
  state: StatusBadgeState | (string & {});
  label?: string;
  withDot?: boolean;
  compact?: boolean;
  title?: string;
  className?: string;
};

export const StatusBadge = forwardRef<HTMLSpanElement, StatusBadgeProps>(function StatusBadge(
  { state, label, withDot = false, compact = false, title, className },
  ref,
) {
  const classes = ["status-badge", `is-${state}`];
  if (withDot) classes.push("has-dot");
  if (compact) classes.push("is-compact");
  if (className) classes.push(className);
  const text = label ?? DEFAULT_LABEL[state as StatusBadgeState] ?? state;
  return (
    <span
      className={classes.join(" ")}
      data-state={state}
      ref={ref}
      role="status"
      title={title ?? text}
    >
      {withDot ? <span aria-hidden="true" className="status-badge-dot" /> : null}
      <span className="status-badge-label">{text}</span>
    </span>
  );
});
