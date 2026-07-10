import { useSyncExternalStore } from "react";
import { formatAbsolute, formatRelative } from "./timestamp-format";

const listeners = new Set<() => void>();
let cachedNow = 0;
let intervalId: ReturnType<typeof setInterval> | null = null;

const TICK_MS = 30_000;

function tick() {
  cachedNow = Date.now();
  for (const notify of listeners) notify();
}

function subscribe(notify: () => void): () => void {
  listeners.add(notify);
  if (intervalId === null) {
    cachedNow = Date.now();
    intervalId = setInterval(tick, TICK_MS);
  }
  return () => {
    listeners.delete(notify);
    if (listeners.size === 0 && intervalId !== null) {
      clearInterval(intervalId);
      intervalId = null;
    }
  };
}

function getSnapshot(): number {
  return cachedNow || Date.now();
}

export function useNowMs(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

export { formatAbsolute, formatRelative };

export function Timestamp({
  value,
  className,
}: {
  value: string;
  className?: string;
}) {
  const nowMs = useNowMs();
  const label = formatRelative(value, nowMs);
  if (!label) return null;
  const classes = className ? `session-ts tabular-nums ${className}` : "session-ts tabular-nums";
  return (
    <time className={classes} dateTime={value} title={formatAbsolute(value)}>
      {label}
    </time>
  );
}
