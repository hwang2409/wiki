import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowDown, ArrowUp, ChevronDown, X } from "lucide-react";
import {
  getAutopilotFleetStatus,
  getCosts,
  getDashboardTickets,
  type AutopilotFleetStatus,
  type CostResponse,
  type DashboardTicket,
} from "./api";
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
import { StatusBadge } from "./status-badge";
import { formatRelative } from "./timestamp-format";

const REFRESH_INTERVAL_MS = 15_000;
const FILTERS_STORAGE_KEY = "wiki-dashboard-filters";

function AutopilotDashboardCard() {
  const [status, setStatus] = useState<AutopilotFleetStatus | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    getAutopilotFleetStatus(controller.signal)
      .then(setStatus)
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  if (!status || typeof status.enabled !== "number") return null;
  return (
    <div className="dashboard-autopilot-card" data-testid="autopilot-dashboard-card">
      <span className="dashboard-autopilot-title">autopilot</span>
      <span>{status.enabled} on</span>
      <span>{status.actions_last_hour} actions / 1h</span>
      <span>{status.halted} halted</span>
    </div>
  );
}

const STATUS_STATE: Record<string, string> = {
  implementing: "working",
  working: "working",
  blocked: "blocked",
  "merge-ready": "merge-ready",
  "pr-open": "pr-open",
  "checks-pending": "checks-pending",
  failing: "failing",
  "has-comments": "has-comments",
  passing: "passing",
  merged: "outcome-merged",
  "merged (local)": "outcome-merged",
  prod: "prod",
  closed: "outcome-closed",
  abandoned: "outcome-abandoned",
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
  fetchCosts?: (signal: AbortSignal) => Promise<CostResponse>;
  pollMs?: number;
};

function fetchDefaultCosts(signal: AbortSignal): Promise<CostResponse> {
  return getCosts({}, signal);
}

export function DashboardView({
  fetchTickets = getDashboardTickets,
  fetchCosts = fetchDefaultCosts,
  pollMs = REFRESH_INTERVAL_MS,
}: DashboardViewProps = {}) {
  const [tickets, setTickets] = useState<DashboardTicket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortAsc, setSortAsc] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [costs, setCosts] = useState<CostResponse | null>(null);
  const [costError, setCostError] = useState<string | null>(null);
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
    const handle = startDashboardPolling({
      fetch: fetchCosts,
      onData: (payload) => {
        setCosts(payload);
        setCostError(null);
      },
      onError: (message) => setCostError(message),
      intervalMs: pollMs,
    });
    return () => handle.stop();
  }, [fetchCosts, pollMs]);

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
      <AutopilotDashboardCard />
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
                    <StatusBadge
                      label={ticket.status}
                      state={STATUS_STATE[ticket.status] ?? "unknown"}
                      title={ticket.detail ?? ticket.status}
                    />
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
      {costError ? <div className="dashboard-error">cost data: {costError}</div> : null}
      {costs ? <CostDashboard costs={costs} /> : null}
    </div>
  );
}

function costLabel(row: { cost_usd: number | null; pricing: string; unpriced_tokens: number }): string {
  if (row.cost_usd === null) return `unpriced · ${row.unpriced_tokens.toLocaleString()} tokens`;
  if (row.pricing === "mixed") return `$${row.cost_usd.toFixed(4)} + unpriced`;
  return `$${row.cost_usd.toFixed(4)}`;
}

function CostTable({ title, rows }: { title: string; rows: CostResponse["top"]["worker"] }) {
  return (
    <section className="cost-panel-section">
      <div className="cost-panel-section-title">{title}</div>
      {rows.length === 0 ? <div className="dashboard-muted">no usage</div> : null}
      {rows.slice(0, 6).map((row) => (
        <div className="cost-row" key={`${title}-${row.label}`}>
          <span className="cost-row-label" title={row.models.join(", ")}>{row.label}</span>
          <span className="cost-row-tokens tabular-nums">{row.total_tokens.toLocaleString()}</span>
          <span className={`cost-row-price is-${row.pricing}`}>{costLabel(row)}</span>
        </div>
      ))}
    </section>
  );
}

function CostDashboard({ costs }: { costs: CostResponse }) {
  const total = costs.totals;
  const maxPromptRuns = Math.max(1, ...costs.prompt_size_distribution.map((item) => item.runs));
  return (
    <section className="cost-dashboard" aria-label="Agent costs">
      <div className="cost-dashboard-header">
        <div>
          <h2>Costs</h2>
          <span className="dashboard-count">{costs.runs_scanned} runs scanned</span>
        </div>
        {costs.refreshing ? <span className="tokens-refreshing">refreshing...</span> : null}
      </div>
      <div className="cost-summary">
        <div><span>spend</span><strong className={`is-${total.pricing}`}>{costLabel(total)}</strong></div>
        <div><span>tokens</span><strong>{total.total_tokens.toLocaleString()}</strong></div>
        <div><span>velocity</span><strong>{costs.velocity.tokens_per_minute.toLocaleString()} / min</strong></div>
      </div>
      <div className="cost-panel-grid">
        <CostTable title="top workers" rows={costs.top.worker} />
        <CostTable title="top tickets" rows={costs.top.ticket} />
        <CostTable title="top orchestrators" rows={costs.top.orchestrator} />
        <CostTable title="by day" rows={costs.top.day} />
      </div>
      <div className="cost-prompt-panel">
        <div className="cost-panel-section-title">prompt size</div>
        <div className="cost-prompt-bars">
          {costs.prompt_size_distribution.map((item) => (
            <div className="cost-prompt-bar" key={item.bucket}>
              <div
                className="cost-prompt-bar-fill"
                style={{ height: `${item.runs > 0 ? Math.min(40, Math.max(4, Math.round((item.runs / maxPromptRuns) * 40))) : 0}px` }}
              />
              <span>{item.bucket}</span>
              <strong>{item.runs}</strong>
            </div>
          ))}
        </div>
      </div>
    </section>
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
