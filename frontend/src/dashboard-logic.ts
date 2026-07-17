import type { DashboardTicket } from "./api";

export type SortKey = "ticket" | "description" | "pr" | "status" | "date";

function fieldValue(ticket: DashboardTicket, key: SortKey): string {
  if (key === "date") return ticket.date ?? "";
  if (key === "pr") return ticket.pr ?? "";
  return ticket[key] ?? "";
}

export function compareTickets(
  a: DashboardTicket,
  b: DashboardTicket,
  key: SortKey,
  asc: boolean
): number {
  const cmp = fieldValue(a, key).localeCompare(fieldValue(b, key));
  if (cmp === 0) return 0;
  return asc ? cmp : -cmp;
}

export type PollingDeps<T> = {
  fetch: (signal: AbortSignal) => Promise<T>;
  onData: (data: T) => void;
  onError: (message: string) => void;
  intervalMs: number;
  setTimeoutFn?: (cb: () => void, ms: number) => number;
  clearTimeoutFn?: (id: number) => void;
};

export type PollingHandle = {
  stop: () => void;
  activeSignal: () => AbortSignal | null;
};

/**
 * Completion-scheduled polling. The next request is only queued after the
 * current one settles, so slow/hung requests never overlap. Every load
 * carries an AbortController that is aborted on stop().
 */
export function startDashboardPolling<T>(deps: PollingDeps<T>): PollingHandle {
  const setTimeoutFn = deps.setTimeoutFn ?? ((cb, ms) => setTimeout(cb, ms) as unknown as number);
  const clearTimeoutFn = deps.clearTimeoutFn ?? ((id) => clearTimeout(id));
  let cancelled = false;
  let timer: number | null = null;
  let controller: AbortController | null = null;

  const scheduleNext = () => {
    if (cancelled) return;
    timer = setTimeoutFn(() => {
      timer = null;
      void load();
    }, deps.intervalMs);
  };

  const load = async () => {
    if (cancelled) return;
    controller?.abort();
    const current = new AbortController();
    controller = current;
    try {
      const data = await deps.fetch(current.signal);
      if (cancelled || current.signal.aborted) return;
      deps.onData(data);
    } catch (err) {
      if (cancelled || current.signal.aborted) return;
      deps.onError(err instanceof Error ? err.message : String(err));
    } finally {
      if (controller === current) controller = null;
      scheduleNext();
    }
  };

  void load();

  return {
    stop() {
      cancelled = true;
      if (timer !== null) {
        clearTimeoutFn(timer);
        timer = null;
      }
      controller?.abort();
      controller = null;
    },
    activeSignal: () => controller?.signal ?? null,
  };
}
