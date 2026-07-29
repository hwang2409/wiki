import { useEffect, useMemo, useState } from "react";
import { getFleetGraph, type FleetGraphGroup, type FleetGraphTicket, type WorkgraphEdge, type WorkgraphNode } from "./api";
import { WorkgraphDag } from "./workgraph-panel";

type FilterKind = "orch" | "state" | "role";

function toggleFilter(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) next.delete(value);
  else next.add(value);
  return next;
}

function filterValues(groups: FleetGraphGroup[], kind: FilterKind): string[] {
  const values = new Set<string>();
  for (const group of groups) {
    if (kind === "orch") values.add(group.orch);
    for (const ticket of group.tickets) {
      if (kind === "state") values.add(ticket.state);
      if (kind === "role") values.add(ticket.role);
    }
  }
  return [...values].sort();
}

function matchesTicket(
  group: FleetGraphGroup,
  ticket: FleetGraphTicket,
  filters: Record<FilterKind, Set<string>>
): boolean {
  return (
    (filters.orch.size === 0 || filters.orch.has(group.orch)) &&
    (filters.state.size === 0 || filters.state.has(ticket.state)) &&
    (filters.role.size === 0 || filters.role.has(ticket.role))
  );
}

function dedupeEdges(tickets: FleetGraphTicket[]): WorkgraphEdge[] {
  const edges = new Map<string, WorkgraphEdge>();
  for (const ticket of tickets) {
    for (const edge of ticket.edges) {
      const key = `${edge.kind}:${edge.from}:${edge.to}:${edge.created_at}`;
      const existing = edges.get(key);
      if (!existing || edge.active) edges.set(key, edge);
    }
  }
  return [...edges.values()].sort((left, right) => left.created_at.localeCompare(right.created_at));
}

function groupNodes(group: FleetGraphGroup): WorkgraphNode[] {
  return [
    { id: `orch:${group.orch}`, kind: "orchestrator", label: `${group.orch} orch` },
    ...group.tickets.map((ticket) => ({
      id: ticket.ticket,
      kind: ticket.role || "worker",
      label: ticket.ticket,
      state: ticket.state,
      worker_id: ticket.ticket,
    })),
  ];
}

function FilterChips({
  label,
  options,
  selected,
  onToggle,
  onClear,
}: {
  label: string;
  options: string[];
  selected: Set<string>;
  onToggle: (value: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="fleet-graph-filter" role="group" aria-label={`${label} filter`}>
      <span className="fleet-graph-filter-label">{label}</span>
      <button
        className={`fleet-graph-chip${selected.size === 0 ? " is-active" : ""}`}
        type="button"
        onClick={onClear}
      >
        all
      </button>
      {options.map((option) => (
        <button
          className={`fleet-graph-chip${selected.has(option) ? " is-active" : ""}`}
          key={option}
          type="button"
          onClick={() => onToggle(option)}
        >
          {option}
        </button>
      ))}
    </div>
  );
}

function FleetGroup({ group }: { group: FleetGraphGroup }) {
  const edges = useMemo(() => dedupeEdges(group.tickets), [group.tickets]);
  return (
    <section className="fleet-graph-group" data-orch={group.orch}>
      <div className="fleet-graph-group-head">
        <h2>{group.orch}</h2>
        <span>{group.tickets.length} workers</span>
      </div>
      <div className="fleet-graph-dag-wrap">
        {edges.length === 0 ? (
          <div className="fleet-graph-empty">no graph edges yet</div>
        ) : (
          <WorkgraphDag
            currentEdge={null}
            edges={edges}
            nodes={groupNodes(group)}
            showAllNodes
          />
        )}
      </div>
    </section>
  );
}

export function FleetGraphView({ refreshTick }: { refreshTick: number }) {
  const [groups, setGroups] = useState<FleetGraphGroup[]>([]);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<FilterKind, Set<string>>>({
    orch: new Set(),
    state: new Set(),
    role: new Set(),
  });

  useEffect(() => {
    let ignore = false;
    getFleetGraph()
      .then((data) => {
        if (ignore) return;
        setGroups(data.groups);
        setUpdatedAt(data.updated_at_ns);
        setError(null);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load fleet graph");
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [refreshTick]);

  const filteredGroups = useMemo(() => {
    return groups
      .map((group) => ({
        ...group,
        tickets: group.tickets.filter((ticket) => matchesTicket(group, ticket, filters)),
      }))
      .filter((group) => group.tickets.length > 0);
  }, [filters, groups]);

  return (
    <main className="fleet-graph-view" data-testid="fleet-graph-view">
      <div className="fleet-graph-header">
        <div>
          <h1>fleet graph</h1>
          <p>live workers and recent archived runs across orchestrators</p>
        </div>
        <span className="fleet-graph-updated">
          {updatedAt ? `updated ${new Date(updatedAt / 1_000_000).toLocaleTimeString()}` : "loading"}
        </span>
      </div>
      <div className="fleet-graph-filters">
        <FilterChips
          label="orch"
          options={filterValues(groups, "orch")}
          selected={filters.orch}
          onClear={() => setFilters((current) => ({ ...current, orch: new Set() }))}
          onToggle={(value) => setFilters((current) => ({ ...current, orch: toggleFilter(current.orch, value) }))}
        />
        <FilterChips
          label="state"
          options={filterValues(groups, "state")}
          selected={filters.state}
          onClear={() => setFilters((current) => ({ ...current, state: new Set() }))}
          onToggle={(value) => setFilters((current) => ({ ...current, state: toggleFilter(current.state, value) }))}
        />
        <FilterChips
          label="role"
          options={filterValues(groups, "role")}
          selected={filters.role}
          onClear={() => setFilters((current) => ({ ...current, role: new Set() }))}
          onToggle={(value) => setFilters((current) => ({ ...current, role: toggleFilter(current.role, value) }))}
        />
      </div>
      {error ? <div className="fleet-graph-error" role="alert">{error}</div> : null}
      {loading && groups.length === 0 ? <div className="fleet-graph-empty">loading fleet graph</div> : null}
      {!loading && filteredGroups.length === 0 ? <div className="fleet-graph-empty">no workers match the current filters</div> : null}
      <div className="fleet-graph-groups">
        {filteredGroups.map((group) => <FleetGroup group={group} key={group.orch} />)}
      </div>
    </main>
  );
}
