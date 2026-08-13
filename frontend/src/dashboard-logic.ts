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

export type DashboardFilters = {
  projects: string[];
  states: string[];
  dateFrom: string | null;
  dateTo: string | null;
};

export function emptyFilters(): DashboardFilters {
  return { projects: [], states: [], dateFrom: null, dateTo: null };
}

export function filtersActive(filters: DashboardFilters): boolean {
  return (
    filters.projects.length > 0 ||
    filters.states.length > 0 ||
    filters.dateFrom !== null ||
    filters.dateTo !== null
  );
}

/**
 * Ticket project prefix: everything before the last `-`. `WIKI-134` → `WIKI`,
 * `MITMWEB-B2` → `MITMWEB`. Falls back to the full ticket string when no `-`.
 */
export function ticketProject(ticket: string): string {
  const idx = ticket.lastIndexOf("-");
  return idx > 0 ? ticket.slice(0, idx) : ticket;
}

export function collectProjects(tickets: DashboardTicket[]): string[] {
  const set = new Set<string>();
  for (const t of tickets) set.add(ticketProject(t.ticket));
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}

export function collectStates(tickets: DashboardTicket[]): string[] {
  const set = new Set<string>();
  for (const t of tickets) set.add(t.status);
  return Array.from(set).sort((a, b) => a.localeCompare(b));
}

function matchesDateBounds(dateIso: string | null, from: string | null, to: string | null): boolean {
  if (from === null && to === null) return true;
  if (!dateIso) return false;
  const day = dateIso.slice(0, 10);
  if (from !== null && day < from) return false;
  if (to !== null && day > to) return false;
  return true;
}

export function ticketMatchesFilters(ticket: DashboardTicket, filters: DashboardFilters): boolean {
  if (filters.projects.length > 0 && !filters.projects.includes(ticketProject(ticket.ticket))) {
    return false;
  }
  if (filters.states.length > 0 && !filters.states.includes(ticket.status)) {
    return false;
  }
  if (!matchesDateBounds(ticket.date, filters.dateFrom, filters.dateTo)) {
    return false;
  }
  return true;
}

export function filterTickets(
  tickets: DashboardTicket[],
  filters: DashboardFilters
): DashboardTicket[] {
  if (!filtersActive(filters)) return tickets;
  return tickets.filter((t) => ticketMatchesFilters(t, filters));
}

export function parseStoredFilters(raw: string | null): DashboardFilters {
  if (!raw) return emptyFilters();
  try {
    const parsed = JSON.parse(raw) as Partial<DashboardFilters> | null;
    if (!parsed || typeof parsed !== "object") return emptyFilters();
    const projects = Array.isArray(parsed.projects)
      ? parsed.projects.filter((v): v is string => typeof v === "string")
      : [];
    const states = Array.isArray(parsed.states)
      ? parsed.states.filter((v): v is string => typeof v === "string")
      : [];
    const dateFrom = typeof parsed.dateFrom === "string" && parsed.dateFrom ? parsed.dateFrom : null;
    const dateTo = typeof parsed.dateTo === "string" && parsed.dateTo ? parsed.dateTo : null;
    return { projects, states, dateFrom, dateTo };
  } catch {
    return emptyFilters();
  }
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
  /**
   * Trigger an immediate refetch, cancelling any in-flight request. Returns
   * a promise that resolves when the manual refetch settles (so callers can
   * flip a "retrying…" affordance off). Used by dashboard retry buttons.
   */
  refresh: () => Promise<void>;
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

  const load = async (): Promise<void> => {
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
      // Only the WINNING load (the one whose controller is still current)
      // schedules the next poll. If refresh() or a later load() superseded
      // us mid-flight, our controller has been abandoned — that superseding
      // load owns the follow-up, and firing scheduleNext here would leave
      // an orphaned timer (its handle would clobber `timer`, making stop()
      // and refresh() unable to cancel the newer one).
      if (controller === current) {
        controller = null;
        scheduleNext();
      }
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
    refresh() {
      if (cancelled) return Promise.resolve();
      if (timer !== null) {
        clearTimeoutFn(timer);
        timer = null;
      }
      return load();
    },
  };
}
