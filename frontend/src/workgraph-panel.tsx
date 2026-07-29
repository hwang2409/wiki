import { useEffect, useMemo, useState } from "react";
import {
  getAgentWorkgraph,
  getAgentWorkgraphRevision,
  getAgentWorkgraphRevisions,
} from "./api";
import type {
  AgentWorkgraphData,
  Workgraph,
  WorkgraphEdge,
  WorkgraphFinding,
  WorkgraphNode,
  WorkgraphRevision,
} from "./api";
import { StatusBadge } from "./status-badge";

const NODE_WIDTH = 148;
const NODE_HEIGHT = 44;
const ROW_GAP = 26;
const LANE_PADDING = 8;
const SVG_WIDTH = 430;

type EdgeGroup = {
  key: string;
  kind: string;
  from: string;
  to: string;
  count: number;
  lastIndex: number;
};

function shortTime(value: string | undefined): string {
  if (!value) return "";
  return value.slice(11, 19) || value;
}

function revisionTime(createdAtNs: number): string {
  return new Date(createdAtNs / 1_000_000).toISOString().replace("T", " ").slice(0, 19) + "Z";
}

function humanStall(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** Latest instance per finding id across edge payloads, chronological order. */
function collectFindings(edges: WorkgraphEdge[]): WorkgraphFinding[] {
  const byId = new Map<string, WorkgraphFinding>();
  for (const edge of edges) {
    for (const finding of edge.payload?.findings ?? []) {
      if (finding?.id) byId.set(finding.id, finding);
    }
  }
  return [...byId.values()];
}

function visibleNodeIds(edges: WorkgraphEdge[]): Set<string> {
  const ids = new Set<string>();
  for (const edge of edges) {
    ids.add(edge.from);
    ids.add(edge.to);
  }
  return ids;
}

function groupEdges(edges: WorkgraphEdge[]): EdgeGroup[] {
  const groups = new Map<string, EdgeGroup>();
  edges.forEach((edge, index) => {
    const key = `${edge.from}->${edge.to}:${edge.kind}`;
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
      existing.lastIndex = index;
    } else {
      groups.set(key, { key, kind: edge.kind, from: edge.from, to: edge.to, count: 1, lastIndex: index });
    }
  });
  return [...groups.values()];
}

function archivedNodeIds(edges: WorkgraphEdge[]): Set<string> {
  return new Set(edges.filter((edge) => edge.kind === "archive").map((edge) => edge.to));
}

type LayoutNode = WorkgraphNode & { x: number; y: number; column: "left" | "right" };

function layoutNodes(nodes: WorkgraphNode[], height: number): Map<string, LayoutNode> {
  const layout = new Map<string, LayoutNode>();
  const left = nodes.filter((node) => node.kind === "orchestrator");
  const right = nodes.filter((node) => node.kind !== "orchestrator");
  left.forEach((node, index) => {
    const slot = height / (left.length + 1);
    layout.set(node.id, {
      ...node,
      column: "left",
      x: LANE_PADDING,
      y: slot * (index + 1) - NODE_HEIGHT / 2,
    });
  });
  right.forEach((node, index) => {
    layout.set(node.id, {
      ...node,
      column: "right",
      x: SVG_WIDTH - NODE_WIDTH - LANE_PADDING,
      y: LANE_PADDING + index * (NODE_HEIGHT + ROW_GAP),
    });
  });
  return layout;
}

function edgeGeometry(
  group: EdgeGroup,
  layout: Map<string, LayoutNode>,
  laneIndex: number
): { path: string; labelX: number; labelY: number } | null {
  const source = layout.get(group.from);
  const target = layout.get(group.to);
  if (!source || !target) return null;
  const nudge = laneIndex * 9;
  if (source.column === target.column) {
    // Same lane (e.g. orch self-loop safety) — arc out to the side.
    const x = source.x + (source.column === "left" ? NODE_WIDTH : 0);
    const y1 = source.y + NODE_HEIGHT / 2;
    const y2 = target.y + NODE_HEIGHT / 2;
    const bow = source.column === "left" ? 40 + nudge : -40 - nudge;
    const midY = (y1 + y2) / 2;
    return {
      path: `M ${x} ${y1} C ${x + bow} ${y1}, ${x + bow} ${y2}, ${x} ${y2}`,
      labelX: x + bow,
      labelY: midY,
    };
  }
  const leftToRight = source.column === "left";
  const x1 = source.x + (leftToRight ? NODE_WIDTH : 0);
  const y1 = source.y + NODE_HEIGHT / 2 + (leftToRight ? nudge : -nudge);
  const x2 = target.x + (leftToRight ? 0 : NODE_WIDTH);
  const y2 = target.y + NODE_HEIGHT / 2 + (leftToRight ? nudge : -nudge);
  const bend = (x2 - x1) * 0.45;
  // Left-to-right labels stack above the curve, right-to-left below — keeps
  // opposing-direction labels apart. Stagger from the un-nudged midpoints so
  // parallel groups between the same pair separate by a full laneIndex step.
  const sourceMid = source.y + NODE_HEIGHT / 2;
  const targetMid = target.y + NODE_HEIGHT / 2;
  const labelX = (x1 + x2) / 2 + (leftToRight ? -24 : 24);
  const labelY = leftToRight
    ? Math.min(sourceMid, targetMid) - 4 - laneIndex * 11
    : Math.max(sourceMid, targetMid) + 13 + laneIndex * 11;
  return {
    path: `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`,
    labelX,
    labelY,
  };
}

function WorkgraphDag({
  currentEdge,
  edges,
  nodes,
}: {
  currentEdge: WorkgraphEdge | null;
  edges: WorkgraphEdge[];
  nodes: WorkgraphNode[];
}) {
  const shown = visibleNodeIds(edges);
  const drawn = nodes.filter((node) => shown.has(node.id));
  const rightCount = drawn.filter((node) => node.kind !== "orchestrator").length;
  const height = Math.max(
    NODE_HEIGHT + LANE_PADDING * 2,
    rightCount * (NODE_HEIGHT + ROW_GAP) - ROW_GAP + LANE_PADDING * 2
  );
  const layout = layoutNodes(drawn, height);
  const groups = groupEdges(edges);
  const archived = archivedNodeIds(edges);
  const currentKey = currentEdge
    ? `${currentEdge.from}->${currentEdge.to}:${currentEdge.kind}`
    : null;

  // Lane index staggers parallel edge groups between the same node pair.
  const laneByPair = new Map<string, number>();

  return (
    <svg
      className="workgraph-dag"
      role="img"
      aria-label="workgraph DAG"
      viewBox={`0 0 ${SVG_WIDTH} ${height}`}
      preserveAspectRatio="xMidYMid meet"
    >
      <defs>
        <marker
          id="workgraph-arrow"
          markerWidth="7"
          markerHeight="7"
          refX="6"
          refY="3.5"
          orient="auto"
        >
          <path className="workgraph-arrowhead" d="M 0 0 L 7 3.5 L 0 7 z" />
        </marker>
      </defs>
      {groups.map((group) => {
        const pair = `${group.from}->${group.to}`;
        const laneIndex = laneByPair.get(pair) ?? 0;
        laneByPair.set(pair, laneIndex + 1);
        const geometry = edgeGeometry(group, layout, laneIndex);
        if (!geometry) return null;
        const isCurrent = currentKey === group.key;
        return (
          <g
            className={`workgraph-edge is-${group.kind}${isCurrent ? " is-current" : ""}`}
            data-edge-kind={group.kind}
            key={group.key}
          >
            <path className="workgraph-edge-line" d={geometry.path} markerEnd="url(#workgraph-arrow)" />
            <text className="workgraph-edge-label" textAnchor="middle" x={geometry.labelX} y={geometry.labelY}>
              {group.kind}
              {group.count > 1 ? ` ×${group.count}` : ""}
            </text>
          </g>
        );
      })}
      {[...layout.values()].map((node) => {
        const isArchived = archived.has(node.id);
        const isCurrent =
          currentEdge !== null && (currentEdge.from === node.id || currentEdge.to === node.id);
        const className = [
          "workgraph-node",
          `is-${node.kind}`,
          isArchived ? "is-archived" : "",
          isCurrent ? "is-current" : "",
        ]
          .filter(Boolean)
          .join(" ");
        return (
          <g className={className} data-node-id={node.id} key={node.id}>
            <title>{`${node.id} · ${node.kind} · ${node.label}`}</title>
            <rect height={NODE_HEIGHT} rx={6} width={NODE_WIDTH} x={node.x} y={node.y} />
            <text className="workgraph-node-kind" x={node.x + 10} y={node.y + 17}>
              {node.kind}
              {isArchived ? " · archived" : ""}
            </text>
            <text className="workgraph-node-label" x={node.x + 10} y={node.y + 33}>
              {node.label.length > 22 ? `${node.label.slice(0, 21)}…` : node.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function HealthStrip({ graph }: { graph: Workgraph }) {
  const health = graph.composite_health;
  return (
    <div className="workgraph-health" data-health-state={health.state}>
      <div className="workgraph-health-cell is-state">
        <StatusBadge compact label={health.state} state={health.state === "merge-ready" ? "merge-ready" : "neutral"} />
        <span className="workgraph-health-label">state</span>
      </div>
      <div className="workgraph-health-cell">
        <span className="workgraph-health-value">{health.open_findings}</span>
        <span className="workgraph-health-label">open</span>
      </div>
      <div className={`workgraph-health-cell${health.blocking > 0 ? " is-alarm" : ""}`}>
        <span className="workgraph-health-value">{health.blocking}</span>
        <span className="workgraph-health-label">blocking</span>
      </div>
      <div className="workgraph-health-cell">
        <span className="workgraph-health-value">{humanStall(health.slowest_node_stall_seconds)}</span>
        <span className="workgraph-health-label">stall</span>
      </div>
      <div className="workgraph-health-cell">
        <span className="workgraph-health-value">{health.iteration_count}</span>
        <span className="workgraph-health-label">rounds</span>
      </div>
    </div>
  );
}

function FindingsTable({ findings }: { findings: WorkgraphFinding[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const open = findings.filter((finding) => !finding.resolved_by);
  if (open.length === 0) {
    return <div className="workgraph-findings-empty">no open findings</div>;
  }
  return (
    <table className="workgraph-findings">
      <thead>
        <tr>
          <th>severity</th>
          <th>id</th>
          <th>finding</th>
        </tr>
      </thead>
      <tbody>
        {open.map((finding) => (
          <FindingRow
            expanded={expanded === finding.id}
            finding={finding}
            key={finding.id}
            onToggle={() => setExpanded((current) => (current === finding.id ? null : finding.id))}
          />
        ))}
      </tbody>
    </table>
  );
}

function FindingRow({
  expanded,
  finding,
  onToggle,
}: {
  expanded: boolean;
  finding: WorkgraphFinding;
  onToggle: () => void;
}) {
  return (
    <>
      <tr className="workgraph-finding-row" data-finding-id={finding.id} onClick={onToggle}>
        <td>
          <span className={`workgraph-severity is-${finding.severity.toLowerCase()}`}>
            {finding.severity.toLowerCase()}
          </span>
        </td>
        <td className="workgraph-finding-id">{finding.id}</td>
        <td className="workgraph-finding-title">
          {finding.title}
          {finding.file ? (
            <span className="workgraph-finding-file">
              {finding.file}
              {finding.line ? `:${finding.line}` : ""}
            </span>
          ) : null}
        </td>
      </tr>
      {expanded ? (
        <tr className="workgraph-finding-detail">
          <td colSpan={3}>
            {finding.observed ? <p><span>observed</span>{finding.observed}</p> : null}
            {finding.why_wrong ? <p><span>why wrong</span>{finding.why_wrong}</p> : null}
            {finding.do_instead ? <p><span>do instead</span>{finding.do_instead}</p> : null}
            <p>
              <span>source</span>
              {finding.source_worker ?? "unknown"}
              {finding.source_sha ? ` @ ${finding.source_sha.slice(0, 7)}` : ""}
            </p>
          </td>
        </tr>
      ) : null}
    </>
  );
}

export function WorkgraphPanel({ ticket, tick }: { ticket: string; tick: number }) {
  const [data, setData] = useState<AgentWorkgraphData | null>(null);
  const [revisions, setRevisions] = useState<WorkgraphRevision[]>([]);
  const [revisionData, setRevisionData] = useState<AgentWorkgraphData | null>(null);
  const [currentRevision, setCurrentRevision] = useState<number | null>(null);
  const [revisionLoading, setRevisionLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revisionError, setRevisionError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<"live" | "replay">("live");
  const [playing, setPlaying] = useState(false);

  useEffect(() => {
    setData(null);
    setRevisions([]);
    setRevisionData(null);
    setCurrentRevision(null);
    setRevisionLoading(false);
    setError(null);
    setRevisionError(null);
    setLoading(true);
    setMode("live");
    setPlaying(false);
  }, [ticket]);

  useEffect(() => {
    let ignore = false;
    getAgentWorkgraph(ticket)
      .then((result) => {
        if (ignore) return;
        setData(result);
        setError(null);
      })
      .catch((err) => {
        if (ignore) return;
        setError(err instanceof Error ? err.message : "Could not load workgraph");
      })
      .finally(() => {
        if (!ignore) setLoading(false);
      });
    return () => {
      ignore = true;
    };
  }, [ticket, tick]);

  useEffect(() => {
    let ignore = false;
    getAgentWorkgraphRevisions(ticket)
      .then((result) => {
        if (ignore) return;
        setRevisions(result);
        setCurrentRevision(result.at(-1)?.revision ?? null);
      })
      .catch((err) => {
        if (!ignore) setRevisionError(err instanceof Error ? err.message : "Could not load timeline");
      });
    return () => {
      ignore = true;
    };
  }, [ticket]);

  useEffect(() => {
    if (mode !== "replay" || currentRevision === null) return;
    let ignore = false;
    const controller = new AbortController();
    setRevisionData(null);
    setRevisionError(null);
    setRevisionLoading(true);
    const timer = window.setTimeout(() => {
      getAgentWorkgraphRevision(ticket, currentRevision, controller.signal)
        .then((result) => {
          if (ignore || controller.signal.aborted) return;
          setRevisionData(result);
          setRevisionError(null);
          setRevisionLoading(false);
        })
        .catch((err) => {
          if (ignore || controller.signal.aborted) return;
          setRevisionData(null);
          setRevisionError(err instanceof Error ? err.message : "Could not load revision");
          setRevisionLoading(false);
        });
    }, 150);
    return () => {
      ignore = true;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [currentRevision, mode, ticket]);

  useEffect(() => {
    if (!playing || mode !== "replay" || currentRevision === null) return;
    const timer = window.setInterval(() => {
      const index = revisions.findIndex((item) => item.revision === currentRevision);
      if (index < 0 || index >= revisions.length - 1) {
        setPlaying(false);
        return;
      }
      setCurrentRevision(revisions[index + 1].revision);
    }, 600);
    return () => window.clearInterval(timer);
  }, [currentRevision, mode, playing, revisions]);

  const replaying = mode === "replay" && revisions.length > 0;
  const selectedData = replaying ? revisionData : data;
  const graph = selectedData?.workgraph ?? null;
  const revisionIndex = currentRevision === null
    ? -1
    : revisions.findIndex((item) => item.revision === currentRevision);
  const latestRevision = revisions.at(-1)?.revision ?? null;
  const shownEdges = useMemo(() => graph?.edges ?? [], [graph]);
  const findings = useMemo(() => collectFindings(shownEdges), [shownEdges]);
  const chromeGraph = graph ?? data?.workgraph ?? null;

  if (loading && !data) return <div className="workgraph-empty">Loading workgraph…</div>;
  if (!chromeGraph || (!graph && !replaying)) {
    return <div className="workgraph-empty">{error ?? "No workgraph recorded for this ticket."}</div>;
  }

  return (
    <div className="workgraph-panel">
      <div className="workgraph-toolbar">
        <div className="workgraph-mode" role="tablist">
          <button
            aria-selected={mode === "live"}
            className={`workgraph-mode-tab${mode === "live" ? " is-active" : ""}`}
            role="tab"
            type="button"
            onClick={() => {
              setPlaying(false);
              setMode("live");
            }}
          >
            live
          </button>
          <button
            aria-selected={mode === "replay"}
            className={`workgraph-mode-tab${mode === "replay" ? " is-active" : ""}`}
            role="tab"
            type="button"
            onClick={() => {
              setCurrentRevision(revisions.at(-1)?.revision ?? null);
              setMode("replay");
            }}
          >
            replay
          </button>
        </div>
        <span className="workgraph-source">{selectedData?.source === "snapshot" ? "snapshot" : "live file"}</span>
        <span className="workgraph-updated">{chromeGraph.orch} · updated {shortTime(chromeGraph.updated_at)}</span>
      </div>

      {mode === "live" ? <HealthStrip graph={chromeGraph} /> : null}

      {replaying ? (
        <div className="workgraph-scrubber">
          <div className="workgraph-scrubber-row">
            <button
              aria-label={playing ? "Pause timeline" : "Play timeline"}
              aria-pressed={playing}
              className="workgraph-play"
              type="button"
              onClick={() => setPlaying((value) => !value)}
            >
              {playing ? "pause" : "play"}
            </button>
            <input
              aria-label="Timeline revision"
              className="workgraph-slider"
              disabled={revisions.length < 2}
              max={Math.max(revisions.length - 1, 0)}
              min={0}
              type="range"
              value={Math.max(revisionIndex, 0)}
              onChange={(event) => {
                setPlaying(false);
                setCurrentRevision(revisions[Number(event.target.value)]?.revision ?? null);
              }}
            />
          </div>
          <div className="workgraph-frame-info">
            r{currentRevision} of {latestRevision} · {revisions[revisionIndex]?.edge_count ?? 0} edges ·{" "}
            {revisions[revisionIndex] ? revisionTime(revisions[revisionIndex].created_at_ns) : ""}
            {revisionError ? ` · ${revisionError}` : ""}
          </div>
        </div>
      ) : null}

      <div className="workgraph-dag-wrap">
        {!graph ? (
          <div className="workgraph-empty is-inline">
            {revisionLoading ? `Loading revision r${currentRevision}…` : revisionError}
          </div>
        ) : shownEdges.length === 0 ? (
          <div className="workgraph-empty is-inline">no edges yet</div>
        ) : (
          <WorkgraphDag currentEdge={null} edges={shownEdges} nodes={graph.nodes} />
        )}
      </div>

      <div className="workgraph-section">
        <div className="workgraph-section-head">
          open findings{replaying ? " at frame" : ""}
        </div>
        <FindingsTable findings={findings} />
      </div>
    </div>
  );
}
