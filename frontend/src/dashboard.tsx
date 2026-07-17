import { useEffect, useMemo, useState } from "react";
import { ArrowDown, ArrowUp } from "lucide-react";
import { getDashboardTickets, type DashboardTicket } from "./api";
import { externalLinkProps } from "./external-links";
import { formatRelative } from "./timestamp-format";

type SortKey = "ticket" | "description" | "pr" | "status" | "date";

const REFRESH_INTERVAL_MS = 15_000;

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

function compareTickets(a: DashboardTicket, b: DashboardTicket, key: SortKey): number {
  if (key === "date") return (a.date ?? "").localeCompare(b.date ?? "");
  if (key === "pr") return (a.pr ?? "").localeCompare(b.pr ?? "");
  return (a[key] ?? "").localeCompare(b[key] ?? "");
}

export function DashboardView() {
  const [tickets, setTickets] = useState<DashboardTicket[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("date");
  const [sortAsc, setSortAsc] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const payload = await getDashboardTickets();
        if (cancelled) return;
        setTickets(payload.tickets);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      }
    };
    void load();
    const interval = window.setInterval(() => {
      setNowMs(Date.now());
      void load();
    }, REFRESH_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  const sorted = useMemo(() => {
    if (!tickets) return [];
    const next = [...tickets].sort((a, b) => compareTickets(a, b, sortKey));
    if (!sortAsc) next.reverse();
    return next;
  }, [tickets, sortKey, sortAsc]);

  function toggleSort(key: SortKey) {
    if (key === sortKey) {
      setSortAsc((current) => !current);
      return;
    }
    setSortKey(key);
    setSortAsc(key !== "date");
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
        {tickets ? <span className="dashboard-count">{tickets.length} tickets</span> : null}
      </div>
      {error ? <div className="dashboard-error">{error}</div> : null}
      {tickets && tickets.length === 0 ? (
        <div className="dashboard-empty">No tickets with workers or PRs yet.</div>
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
