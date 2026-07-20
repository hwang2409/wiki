import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ChevronDown, X } from "lucide-react";
import { getDashboardTickets, type DashboardTicket } from "./api";
import {
  collectProjects,
  collectStates,
  compareTickets,
  emptyFilters,
  filterTickets,
  filtersActive,
  parseStoredFilters,
  startDashboardPolling,
  type DashboardFilters,
  type SortKey,
} from "./dashboard-logic";
import { externalLinkProps } from "./external-links";
import { formatRelative } from "./timestamp-format";

const REFRESH_INTERVAL_MS = 15_000;
const FILTERS_STORAGE_KEY = "wiki-dashboard-filters";

const STATUS_CLASS: Record<string, string> = {
  implementing: "is-working",
  working: "is-working",
  blocked: "is-blocked",
  "merge-ready": "is-merge-ready",
  "pr-open": "is-pr-open",
  "checks-pending": "is-checks-pending",
  failing: "is-failing",
  "has-comments": "is-has-comments",
  passing: "is-passing",
  merged: "is-outcome-merged",
  "merged (local)": "is-outcome-merged",
  prod: "is-prod",
  closed: "is-outcome-closed",
  abandoned: "is-outcome-abandoned",
};

function prNumber(url: string): string {
  const match = url.match(/\/pull\/(\d+)$/);
  return match ? `#${match[1]}` : url;
}

export type DashboardTicketsPayload = {
  tickets: DashboardTicket[];
  repo_allowlist: string[];
};

export type DashboardViewProps = {
  fetchTickets?: (signal: AbortSignal) => Promise<DashboardTicketsPayload>;
  pollMs?: number;
};

export function DashboardView({
  fetchTickets = getDashboardTickets,
  pollMs = REFRESH_INTERVAL_MS,
}: DashboardViewProps = {}) {
  const [tickets, setTickets] = useState<DashboardTicket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortAsc, setSortAsc] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [filters, setFilters] = useState<DashboardFilters>(() =>
    typeof localStorage === "undefined"
      ? emptyFilters()
      : parseStoredFilters(localStorage.getItem(FILTERS_STORAGE_KEY))
  );

  useEffect(() => {
    const handle = startDashboardPolling({
      fetch: (signal) => fetchTickets(signal),
      onData: (payload) => {
        setTickets(payload.tickets);
        setError(null);
        setNowMs(Date.now());
      },
      onError: (message) => setError(message),
      intervalMs: pollMs,
    });
    return () => handle.stop();
  }, [fetchTickets, pollMs]);

  useEffect(() => {
    if (typeof localStorage === "undefined") return;
    if (filtersActive(filters)) {
      localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters));
    } else {
      localStorage.removeItem(FILTERS_STORAGE_KEY);
    }
  }, [filters]);

  const availableProjects = useMemo(() => (tickets ? collectProjects(tickets) : []), [tickets]);
  const availableStates = useMemo(() => (tickets ? collectStates(tickets) : []), [tickets]);

  const filtered = useMemo(() => {
    if (!tickets) return [];
    return filterTickets(tickets, filters);
  }, [tickets, filters]);

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => compareTickets(a, b, sortKey, sortAsc));
  }, [filtered, sortKey, sortAsc]);

  const isFiltered = filtersActive(filters);
  const totalCount = tickets?.length ?? 0;
  const filteredCount = filtered.length;

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortAsc((current) => !current);
      return;
    }
    setSortKey(key);
    setSortAsc(key !== "date");
  }

  function toggleMulti(kind: "projects" | "states", value: string) {
    setFilters((prev) => {
      const current = prev[kind];
      const next = current.includes(value)
        ? current.filter((v) => v !== value)
        : [...current, value];
      return { ...prev, [kind]: next };
    });
  }

  function clearFilters() {
    setFilters(emptyFilters());
  }

  function header(key: SortKey, label: string) {
    const active = key === sortKey;
    return (
      <th>
        <button type="button" onClick={() => toggleSort(key)}>
          <span>{label}</span>
          {active ? sortAsc ? <ArrowUp size={12} /> : <ArrowDown size={12} /> : null}
        </button>
      </th>
    );
  }

  return (
    <div className="dashboard-view">
      <div className="dashboard-header">
        <h2>Tickets</h2>
        {tickets ? (
          <span className="dashboard-count">
            {isFiltered ? `${filteredCount} / ${totalCount}` : totalCount} tickets
          </span>
        ) : null}
        {tickets ? (
          <DashboardFilterBar
            filters={filters}
            projects={availableProjects}
            states={availableStates}
            onToggleProject={(value) => toggleMulti("projects", value)}
            onToggleState={(value) => toggleMulti("states", value)}
            onDateFromChange={(value) =>
              setFilters((prev) => ({ ...prev, dateFrom: value || null }))
            }
            onDateToChange={(value) =>
              setFilters((prev) => ({ ...prev, dateTo: value || null }))
            }
            onClear={clearFilters}
          />
        ) : null}
      </div>
      {error ? <div className="dashboard-error">{error}</div> : null}
      {tickets && tickets.length === 0 ? (
        <div className="dashboard-empty">No tickets with workers or PRs yet.</div>
      ) : null}
      {tickets && tickets.length > 0 && sorted.length === 0 ? (
        <div className="dashboard-empty">No tickets match the current filters.</div>
      ) : null}
      {sorted.length > 0 ? (
        <div className="artifact-table-scroll dashboard-table-scroll">
          <table className="artifact-table">
            <thead>
              <tr>
                {header("ticket", "Ticket")}
                {header("description", "Description")}
                {header("pr", "PR")}
                {header("status", "Status")}
                {header("date", "Date")}
              </tr>
            </thead>
            <tbody>
              {sorted.map((ticket) => (
                <tr key={ticket.ticket}>
                  <td className="dashboard-ticket">{ticket.ticket}</td>
                  <td className="dashboard-description" title={ticket.description}>
                    {ticket.description}
                  </td>
                  <td>
                    {ticket.pr ? (
                      <a href={ticket.pr} title={ticket.pr} {...externalLinkProps(ticket.pr)}>
                        {prNumber(ticket.pr)}
                      </a>
                    ) : (
                      <span className="dashboard-muted">—</span>
                    )}
                  </td>
                  <td>
                    <span
                      className={`agent-state ${STATUS_CLASS[ticket.status] ?? "is-unknown"}`}
                      title={ticket.detail ?? undefined}
                    >
                      {ticket.status}
                    </span>
                  </td>
                  <td className="dashboard-date" title={ticket.date ?? undefined}>
                    {ticket.date ? formatRelative(ticket.date, nowMs) : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

type DashboardFilterBarProps = {
  filters: DashboardFilters;
  projects: string[];
  states: string[];
  onToggleProject: (value: string) => void;
  onToggleState: (value: string) => void;
  onDateFromChange: (value: string) => void;
  onDateToChange: (value: string) => void;
  onClear: () => void;
};

function DashboardFilterBar({
  filters,
  projects,
  states,
  onToggleProject,
  onToggleState,
  onDateFromChange,
  onDateToChange,
  onClear,
}: DashboardFilterBarProps) {
  const isFiltered = filtersActive(filters);
  return (
    <div className="dashboard-filters" role="group" aria-label="Ticket filters">
      <MultiSelectDropdown
        label="Project"
        options={projects}
        selected={filters.projects}
        onToggle={onToggleProject}
      />
      <MultiSelectDropdown
        label="State"
        options={states}
        selected={filters.states}
        onToggle={onToggleState}
      />
      <div className="dashboard-filter-date-range">
        <label className="dashboard-filter-date">
          <span>From</span>
          <input
            type="date"
            value={filters.dateFrom ?? ""}
            onChange={(event) => onDateFromChange(event.target.value)}
            aria-label="Filter tickets from date"
          />
        </label>
        <label className="dashboard-filter-date">
          <span>To</span>
          <input
            type="date"
            value={filters.dateTo ?? ""}
            onChange={(event) => onDateToChange(event.target.value)}
            aria-label="Filter tickets to date"
          />
        </label>
      </div>
      {isFiltered ? (
        <button
          type="button"
          className="dashboard-filter-clear"
          onClick={onClear}
          title="Clear all filters"
        >
          <X size={12} />
          <span>Clear</span>
        </button>
      ) : null}
    </div>
  );
}

type MultiSelectDropdownProps = {
  label: string;
  options: string[];
  selected: string[];
  onToggle: (value: string) => void;
};

function MultiSelectDropdown({ label, options, selected, onToggle }: MultiSelectDropdownProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDown(event: MouseEvent) {
      if (!rootRef.current) return;
      if (event.target instanceof Node && !rootRef.current.contains(event.target)) {
        setOpen(false);
      }
    }
    function onEsc(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  const summary =
    selected.length === 0
      ? label
      : selected.length === 1
        ? `${label}: ${selected[0]}`
        : `${label}: ${selected.length}`;

  const disabled = options.length === 0;

  return (
    <div className="dashboard-filter-multi" ref={rootRef}>
      <button
        type="button"
        className={`dashboard-filter-trigger${selected.length > 0 ? " is-active" : ""}`}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
      >
        <span>{summary}</span>
        <ChevronDown size={12} />
      </button>
      {open ? (
        <div className="dashboard-filter-popover" role="listbox" aria-label={`${label} filter`}>
          {options.map((option) => {
            const checked = selected.includes(option);
            return (
              <label key={option} className="dashboard-filter-option">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => onToggle(option)}
                />
                <span>{option}</span>
              </label>
            );
          })}
        </div>
      ) : null}
    </div>
  );
}
