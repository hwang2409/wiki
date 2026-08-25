import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CircleDot, LayoutGrid, ListTree, Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import { getLinks } from "./api";
import { GraphCanvas } from "./graph-canvas";
import type { GraphCanvasControls, GraphNodeSummary, LinksMap } from "./graph-canvas";
import { UtilityEmpty, UtilityError, UtilityLoading, UtilityPage } from "./utility-page";

type GraphMode = "canvas" | "list";

export const GRAPH_DENSITY_THRESHOLD = 72;
const GRAPH_MODE_STORAGE_KEY = "wiki-graph-mode";

function readStoredGraphMode(): GraphMode | null {
  try {
    const stored = localStorage.getItem(GRAPH_MODE_STORAGE_KEY);
    return stored === "canvas" || stored === "list" ? stored : null;
  } catch {
    return null;
  }
}

function storeGraphMode(mode: GraphMode) {
  try {
    localStorage.setItem(GRAPH_MODE_STORAGE_KEY, mode);
  } catch {
    // The mode still works for this session when storage is unavailable.
  }
}

function labelOf(id: string) {
  return id.split("/").pop()?.replace(/\.md$/, "") ?? id;
}

function summariseLinks(links: LinksMap): GraphNodeSummary[] {
  const summary = new Map<string, GraphNodeSummary>();

  function ensure(id: string, unresolved: boolean) {
    const existing = summary.get(id);
    if (existing) {
      // A previously-unresolved ghost becomes resolved if we later see it as a real note.
      if (existing.unresolved && !unresolved) existing.unresolved = false;
      return existing;
    }
    const created: GraphNodeSummary = {
      id,
      label: unresolved ? id.replace(/^unresolved:/, "") : labelOf(id),
      unresolved,
      degree: 0,
      outgoing: [],
      incoming: [],
    };
    summary.set(id, created);
    return created;
  }

  for (const source of Object.keys(links)) ensure(source, false);
  for (const [source, entry] of Object.entries(links)) {
    const sourceNode = ensure(source, false);
    for (const target of entry.outgoing) {
      const targetNode = ensure(target, false);
      sourceNode.outgoing.push(target);
      sourceNode.degree += 1;
      targetNode.incoming.push(source);
      targetNode.degree += 1;
    }
    for (const ghost of entry.unresolved) {
      const ghostId = `unresolved:${ghost}`;
      const ghostNode = ensure(ghostId, true);
      sourceNode.outgoing.push(ghostId);
      sourceNode.degree += 1;
      ghostNode.incoming.push(source);
      ghostNode.degree += 1;
    }
  }

  return Array.from(summary.values());
}

export function GraphView({
  onOpenNote,
  refreshTick = 0,
}: {
  onOpenNote: (path: string) => void;
  refreshTick?: number;
}) {
  const [links, setLinks] = useState<LinksMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [mode, setMode] = useState<GraphMode | null>(() => readStoredGraphMode());
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const canvasControlsRef = useRef<GraphCanvasControls | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    getLinks()
      .then((result) => {
        if (!cancelled) {
          setLinks(result);
          setError(null);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Could not load link graph");
      });
    return () => {
      cancelled = true;
    };
  }, [refreshTick, retryTick]);

  const retry = useCallback(() => {
    setError(null);
    setRetryTick((tick) => tick + 1);
  }, []);

  const summaries = useMemo(() => (links ? summariseLinks(links) : []), [links]);
  // Notes and unresolved targets are separate counts — a wikilink to a note
  // that does not exist yet is a *target*, not a note in the vault.
  const noteCount = links ? Object.keys(links).length : 0;
  const unresolvedCount = summaries.filter((node) => node.unresolved).length;
  const denseGraph = summaries.length > GRAPH_DENSITY_THRESHOLD;
  const activeMode: GraphMode | null =
    links === null ? null : mode ?? (denseGraph ? "list" : "canvas");

  const selectMode = useCallback((nextMode: GraphMode) => {
    setMode(nextMode);
    storeGraphMode(nextMode);
  }, []);

  const subtitle =
    links === null
      ? "Note-to-note links across the vault."
      : `${noteCount} ${noteCount === 1 ? "note" : "notes"} · ${unresolvedCount} unresolved ${
          unresolvedCount === 1 ? "target" : "targets"
        }.`;

  const actions = (
    <>
      <div className="graph-focus-hint" aria-live="polite">
        {focusedId ? (
          <>
            <CircleDot size={12} aria-hidden="true" />
            <span title={focusedId}>{labelOf(focusedId.replace(/^unresolved:/, ""))}</span>
          </>
        ) : (
          <span className="graph-focus-hint-idle">no node focused</span>
        )}
      </div>
      <div className="graph-mode-group" role="group" aria-label="Graph view mode">
        <button
          type="button"
          className={`tokens-chip graph-mode-chip${activeMode === "canvas" ? " is-active" : ""}`}
          aria-pressed={activeMode === "canvas"}
          onClick={() => selectMode("canvas")}
        >
          <LayoutGrid size={12} aria-hidden="true" />
          <span>Canvas</span>
        </button>
        <button
          type="button"
          className={`tokens-chip graph-mode-chip${activeMode === "list" ? " is-active" : ""}`}
          aria-pressed={activeMode === "list"}
          onClick={() => selectMode("list")}
        >
          <ListTree size={12} aria-hidden="true" />
          <span>List</span>
        </button>
      </div>
      {activeMode === "canvas" ? (
        <div className="graph-canvas-controls" role="group" aria-label="Canvas controls">
          <button
            type="button"
            className="tokens-chip graph-control-chip"
            aria-label="Zoom out"
            title="Zoom out"
            onClick={() => canvasControlsRef.current?.zoomOut()}
          >
            <ZoomOut size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="tokens-chip graph-control-chip"
            aria-label="Zoom in"
            title="Zoom in"
            onClick={() => canvasControlsRef.current?.zoomIn()}
          >
            <ZoomIn size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            className="tokens-chip graph-control-chip graph-fit-control"
            onClick={() => canvasControlsRef.current?.fit()}
          >
            <Maximize2 size={13} aria-hidden="true" />
            <span>Fit graph</span>
          </button>
        </div>
      ) : null}
    </>
  );

  return (
    <UtilityPage
      title="Graph view"
      subtitle={subtitle}
      actions={actions}
      scroll={false}
      bodyClassName="graph-page-body"
    >
      {links === null && error ? (
        <UtilityError
          title="Link graph is unavailable"
          message={error}
          onRetry={retry}
        />
      ) : links === null ? (
        <UtilityLoading label="Building link graph…" />
      ) : summaries.length === 0 ? (
        <UtilityEmpty
          title="No linked notes yet"
          message="Add [[wikilinks]] between notes to see the graph populate."
        />
      ) : activeMode === "canvas" ? (
        <GraphCanvas
          links={links}
          summaries={summaries}
          dense={denseGraph}
          focusedId={focusedId}
          onFocus={setFocusedId}
          onOpenNote={onOpenNote}
          controlsRef={canvasControlsRef}
        />
      ) : (
        <GraphList summaries={summaries} onFocus={setFocusedId} onOpenNote={onOpenNote} />
      )}
    </UtilityPage>
  );
}

function GraphList({
  summaries,
  onFocus,
  onOpenNote,
}: {
  summaries: GraphNodeSummary[];
  onFocus: (id: string) => void;
  onOpenNote: (path: string) => void;
}) {
  const sorted = useMemo(
    () => [...summaries].sort((a, b) => b.degree - a.degree || a.label.localeCompare(b.label)),
    [summaries],
  );

  const labelFor = useMemo(() => {
    const map = new Map<string, string>();
    for (const node of summaries) map.set(node.id, node.label);
    return (id: string) => map.get(id) ?? labelOf(id.replace(/^unresolved:/, ""));
  }, [summaries]);

  return (
    <div className="graph-list-wrap">
      <p className="graph-list-hint">
        Non-visual fallback — every node in the graph plus its outgoing and incoming
        wikilinks. Enter opens the note; unresolved targets have no note to open.
      </p>
      <ul className="graph-list" role="list">
        {sorted.map((node) => {
          const outgoing = node.outgoing;
          const incoming = node.incoming;
          return (
            <li key={node.id}>
              <details
                className={`graph-list-item${node.unresolved ? " is-unresolved" : ""}`}
                onToggle={(event) => {
                  if ((event.target as HTMLDetailsElement).open) onFocus(node.id);
                }}
              >
                <summary className="graph-list-row">
                  <span className="graph-list-name">{node.label}</span>
                  <span className="graph-list-meta">
                    {node.unresolved
                      ? "unresolved target"
                      : `${node.degree} link${node.degree === 1 ? "" : "s"} · ${outgoing.length} out · ${incoming.length} in`}
                  </span>
                  {!node.unresolved ? (
                    <span className="graph-list-path">{node.id}</span>
                  ) : null}
                </summary>
                <div className="graph-list-body">
                  {!node.unresolved ? (
                    <div className="graph-list-actions">
                      <button
                        type="button"
                        className="graph-list-open"
                        onClick={() => onOpenNote(node.id)}
                      >
                        Open note
                      </button>
                    </div>
                  ) : null}
                  <EdgeList
                    heading="Outgoing wikilinks"
                    ids={outgoing}
                    emptyLabel="No outgoing links from this note."
                    labelFor={labelFor}
                    onOpen={(id) => {
                      if (!id.startsWith("unresolved:")) onOpenNote(id);
                    }}
                  />
                  <EdgeList
                    heading="Incoming wikilinks"
                    ids={incoming}
                    emptyLabel={
                      node.unresolved
                        ? "Nothing links to this unresolved target."
                        : "No notes currently link to this one."
                    }
                    labelFor={labelFor}
                    onOpen={(id) => {
                      if (!id.startsWith("unresolved:")) onOpenNote(id);
                    }}
                  />
                </div>
              </details>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

function EdgeList({
  heading,
  ids,
  emptyLabel,
  labelFor,
  onOpen,
}: {
  heading: string;
  ids: string[];
  emptyLabel: string;
  labelFor: (id: string) => string;
  onOpen: (id: string) => void;
}) {
  return (
    <div className="graph-edge-group" role="group" aria-label={heading}>
      <div className="graph-edge-heading">{heading}</div>
      {ids.length === 0 ? (
        <div className="graph-edge-empty">{emptyLabel}</div>
      ) : (
        <ul className="graph-edge-list" role="list">
          {ids.map((id) => {
            const unresolved = id.startsWith("unresolved:");
            return (
              <li key={`${heading}-${id}`}>
                <button
                  type="button"
                  className={`graph-edge${unresolved ? " is-unresolved" : ""}`}
                  disabled={unresolved}
                  onClick={() => onOpen(id)}
                >
                  <span className="graph-edge-name">{labelFor(id)}</span>
                  {unresolved ? (
                    <span className="graph-edge-flag">unresolved</span>
                  ) : (
                    <span className="graph-edge-path">{id}</span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
