import type { ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";
import { LoadingPlaceholder } from "./loading";

type UtilityPageProps = {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  bodyClassName?: string;
  contentClassName?: string;
  scroll?: boolean;
};

export function UtilityPage({
  title,
  subtitle,
  actions,
  children,
  bodyClassName,
  contentClassName,
  scroll = true,
}: UtilityPageProps) {
  const bodyClasses = [
    "utility-page-body",
    scroll ? "is-scroll" : "is-flex",
    bodyClassName ?? "",
  ]
    .filter(Boolean)
    .join(" ");
  const contentClasses = ["utility-page-content", contentClassName ?? ""]
    .filter(Boolean)
    .join(" ");

  return (
    <section className="utility-page" aria-label={title}>
      <header className="utility-page-header">
        <div className="utility-page-heading">
          <h1 className="utility-page-title">{title}</h1>
          {subtitle ? <p className="utility-page-subtitle">{subtitle}</p> : null}
        </div>
        {actions ? <div className="utility-page-actions">{actions}</div> : null}
      </header>
      <div className={bodyClasses}>
        <div className={contentClasses}>{children}</div>
      </div>
    </section>
  );
}

export function UtilityLoading({
  label = "Loading…",
  lines = [88, 74, 82, 68],
}: {
  label?: string;
  lines?: number[];
}) {
  return (
    <div className="utility-state utility-state-loading" role="status" aria-live="polite">
      <LoadingPlaceholder lines={lines} />
      <span className="utility-state-hint">{label}</span>
    </div>
  );
}

export function UtilityEmpty({
  title,
  message,
  action,
}: {
  title: string;
  message?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="utility-state utility-state-empty" role="status">
      <div className="utility-state-title">{title}</div>
      {message ? <div className="utility-state-message">{message}</div> : null}
      {action ? <div className="utility-state-actions">{action}</div> : null}
    </div>
  );
}

export function UtilityError({
  title = "Could not load",
  message,
  onRetry,
  retryLabel = "Retry",
}: {
  title?: string;
  message?: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="utility-state utility-state-error" role="alert">
      <div className="utility-state-icon">
        <AlertTriangle size={16} />
      </div>
      <div className="utility-state-body">
        <div className="utility-state-title">{title}</div>
        {message ? <div className="utility-state-message">{message}</div> : null}
      </div>
      {onRetry ? (
        <button
          className="utility-state-retry"
          type="button"
          onClick={onRetry}
        >
          <RefreshCw size={13} />
          <span>{retryLabel}</span>
        </button>
      ) : null}
    </div>
  );
}
