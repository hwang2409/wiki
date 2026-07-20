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

function isUnhealthy(entry: ProviderHealthEntry | undefined): boolean {
  return Boolean(entry && entry.status === "unauthorized");
}

function badgeText(kind: string): string {
  const label = KIND_LABEL[kind] ?? kind;
  return `${label} auth dead`;
}

function badgeTitle(entry: ProviderHealthEntry): string {
  const detail = entry.detail?.trim();
  const stamp = entry.checked_at ? ` · checked ${entry.checked_at}` : "";
  return `${detail ?? "provider auth unhealthy"}${stamp}`;
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

  if (!snapshot) return null;
  const unhealthy = Object.entries(snapshot).filter(([, entry]) => isUnhealthy(entry));
  if (unhealthy.length === 0) return null;

  return (
    <div className="provider-health-badges" data-testid="provider-health-badges">
      {unhealthy.map(([kind, entry]) => (
        <span
          className="provider-health-badge is-unauthorized"
          data-provider={kind}
          key={kind}
          title={badgeTitle(entry)}
        >
          {badgeText(kind)}
        </span>
      ))}
    </div>
  );
}
