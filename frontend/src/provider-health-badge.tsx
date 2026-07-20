import { useEffect, useState } from "react";
import {
  getProviderHealth,
  type ProviderHealthEntry,
  type ProviderHealthSnapshot,
} from "./api";
import { startDashboardPolling } from "./dashboard-logic";

const DEFAULT_POLL_MS = 60_000;

const KIND_LABEL: Record<string, string> = {
  cdx: "cdx",
  cc: "cc",
};

export type ProviderHealthBadgeProps = {
  fetchHealth?: (signal: AbortSignal) => Promise<ProviderHealthSnapshot>;
  pollMs?: number;
};

function badgeText(kind: string, status: ProviderHealthEntry["status"]): string {
  const label = KIND_LABEL[kind] ?? kind;
  if (status === "unauthorized") return `${label} auth unavailable`;
  return `${label} auth status unknown`;
}

function badgeTitle(kind: string, status: ProviderHealthEntry["status"]): string {
  if (status === "unauthorized") {
    return `provider auth is unavailable; run ${kind === "cdx" ? "codex" : "claude"} login, then retry`;
  }
  return "provider auth status is unknown; retry or sign in again";
}

export function ProviderHealthBadge({
  fetchHealth = getProviderHealth,
  pollMs = DEFAULT_POLL_MS,
}: ProviderHealthBadgeProps = {}) {
  const [snapshot, setSnapshot] = useState<ProviderHealthSnapshot | null>(null);

  useEffect(() => {
    const handle = startDashboardPolling<ProviderHealthSnapshot>({
      fetch: (signal) => fetchHealth(signal),
      onData: (data) => setSnapshot(data),
      onError: () => {
        // Endpoint failures don't clear the badge; keep the last known state.
      },
      intervalMs: pollMs,
    });
    return () => handle.stop();
  }, [fetchHealth, pollMs]);

  if (!snapshot) {
    return (
      <div
        className="provider-health-badges"
        data-state="probing"
        data-testid="provider-health-badges"
        role="status"
        aria-live="polite"
      >
        <span className="provider-health-badge is-probing">checking provider auth</span>
      </div>
    );
  }
  const unhealthy = Object.entries(snapshot).filter(([, entry]) => entry.status !== "ok");
  if (unhealthy.length === 0) return null;

  return (
    <div
      className="provider-health-badges"
      data-testid="provider-health-badges"
      role="status"
      aria-live="polite"
    >
      {unhealthy.map(([kind, entry]) => (
        <span
          className={`provider-health-badge is-${entry.status}`}
          data-provider={kind}
          key={kind}
          title={badgeTitle(kind, entry.status)}
        >
          {badgeText(kind, entry.status)}
        </span>
      ))}
    </div>
  );
}
