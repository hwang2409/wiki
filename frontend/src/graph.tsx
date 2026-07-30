import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CircleDot, LayoutGrid, ListTree } from "lucide-react";
import { getLinks } from "./api";
import type { NoteLinks } from "./api";
import { UtilityEmpty, UtilityError, UtilityLoading, UtilityPage } from "./utility-page";

type LinksMap = Record<string, NoteLinks>;
type GraphMode = "canvas" | "list";

type GraphNodeSummary = {
  id: string;
  label: string;
  unresolved: boolean;
  degree: number;
  outgoing: string[];
  incoming: string[];
};

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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

export function GraphView({ onOpenNote }: { onOpenNote: (path: string) => void }) {
  const [links, setLinks] = useState<LinksMap | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retryTick, setRetryTick] = useState(0);
  const [mode, setMode] = useState<GraphMode>("canvas");
  const [focusedId, setFocusedId] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
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
  }, [retryTick]);

  const retry = useCallback(() => {
    setError(null);
    setLinks(null);
    setRetryTick((tick) => tick + 1);
  }, []);

  const summaries = useMemo(() => (links ? summariseLinks(links) : []), [links]);
  const nodeCount = summaries.length;
  const unresolvedCount = summaries.filter((node) => node.unresolved).length;

  const subtitle =
    links === null
      ? "Note-to-note links across the vault."
      : `${nodeCount} notes, ${unresolvedCount} unresolved links.`;

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
          className={`tokens-chip graph-mode-chip${mode === "canvas" ? " is-active" : ""}`}
          aria-pressed={mode === "canvas"}
          onClick={() => setMode("canvas")}
        >
          <LayoutGrid size={12} aria-hidden="true" />
          <span>Canvas</span>
        </button>
        <button
          type="button"
          className={`tokens-chip graph-mode-chip${mode === "list" ? " is-active" : ""}`}
          aria-pressed={mode === "list"}
          onClick={() => setMode("list")}
        >
          <ListTree size={12} aria-hidden="true" />
          <span>List</span>
        </button>
      </div>
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
      {error ? (
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
      ) : mode === "canvas" ? (
        <GraphCanvas
          links={links}
          summaries={summaries}
          focusedId={focusedId}
          onFocus={setFocusedId}
          onOpenNote={onOpenNote}
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

  return (
    <div className="graph-list-wrap">
      <p className="graph-list-hint">
        Non-visual fallback — every node in the graph, ranked by connection count. Enter opens the
        note; unresolved links have no target.
      </p>
      <ul className="graph-list" role="list">
        {sorted.map((node) => (
          <li key={node.id}>
            <button
              type="button"
              className={`graph-list-row${node.unresolved ? " is-unresolved" : ""}`}
              disabled={node.unresolved}
              onFocus={() => onFocus(node.id)}
              onClick={() => {
                if (!node.unresolved) onOpenNote(node.id);
              }}
            >
              <span className="graph-list-name">{node.label}</span>
              <span className="graph-list-meta">
                {node.unresolved ? "unresolved" : `${node.degree} link${node.degree === 1 ? "" : "s"}`}
              </span>
              {!node.unresolved ? (
                <span className="graph-list-path">{node.id}</span>
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

type PhysicsNode = {
  id: string;
  label: string;
  unresolved: boolean;
  degree: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
};

type PhysicsEdge = {
  a: number;
  b: number;
};

function GraphCanvas({
  links,
  summaries,
  focusedId,
  onFocus,
  onOpenNote,
}: {
  links: LinksMap;
  summaries: GraphNodeSummary[];
  focusedId: string | null;
  onFocus: (id: string | null) => void;
  onOpenNote: (path: string) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const focusedIdRef = useRef<string | null>(focusedId);
  const openNoteRef = useRef(onOpenNote);
  const focusHandleRef = useRef<((id: string | null) => void) | null>(null);
  const keyboardHandleRef = useRef<((key: string) => void) | null>(null);
  const orderedIdsRef = useRef<string[]>([]);
  const [instructionsShown, setInstructionsShown] = useState(false);

  useEffect(() => {
    focusedIdRef.current = focusedId;
  }, [focusedId]);

  useEffect(() => {
    openNoteRef.current = onOpenNote;
  }, [onOpenNote]);

  useEffect(() => {
    orderedIdsRef.current = [...summaries]
      .sort((a, b) => b.degree - a.degree || a.label.localeCompare(b.label))
      .map((node) => node.id);
  }, [summaries]);

  useEffect(() => {
    const trackRaf = import.meta.env.DEV;
    const metricsWindow = window as typeof window & { __wikiGraphRafCount?: number };
    if (trackRaf) metricsWindow.__wikiGraphRafCount = 0;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const context = canvas.getContext("2d");
    if (!context) return;

    let nodes: PhysicsNode[] = [];
    let edges: PhysicsEdge[] = [];
    let raf = 0;
    let running = false;
    let alpha = 1;
    let hovered: PhysicsNode | null = null;
    let dragged: PhysicsNode | null = null;
    let dragOrigin = { x: 0, y: 0 };
    let dragTravel = 0;
    let disposed = false;
    let hidden = document.visibilityState === "hidden";
    let width = 0;
    let height = 0;
    const view = { scale: 1, ox: 0, oy: 0 };
    const settleAlpha = 0.003;
    let focusedNode: PhysicsNode | null = null;

    function findNode(id: string | null): PhysicsNode | null {
      if (!id) return null;
      return nodes.find((node) => node.id === id) ?? null;
    }

    function toScreenX(x: number) {
      return x * view.scale + view.ox;
    }

    function toScreenY(y: number) {
      return y * view.scale + view.oy;
    }

    function toWorld(px: number, py: number) {
      return { x: (px - view.ox) / view.scale, y: (py - view.oy) / view.scale };
    }

    function fitView() {
      if (nodes.length === 0) return false;
      let minX = Infinity;
      let maxX = -Infinity;
      let minY = Infinity;
      let maxY = -Infinity;
      for (const node of nodes) {
        minX = Math.min(minX, node.x);
        maxX = Math.max(maxX, node.x);
        minY = Math.min(minY, node.y);
        maxY = Math.max(maxY, node.y);
      }
      const pad = 90;
      const spanX = Math.max(1, maxX - minX);
      const spanY = Math.max(1, maxY - minY);
      const target = Math.min(
        (width - pad * 2) / spanX,
        (height - pad * 2) / spanY,
        2.2,
      );
      const targetOx = (width - (minX + maxX) * target) / 2;
      const targetOy = (height - (minY + maxY) * target) / 2;
      const scaleDelta = target - view.scale;
      const oxDelta = targetOx - view.ox;
      const oyDelta = targetOy - view.oy;
      const settled =
        Math.abs(scaleDelta) <= 0.002 &&
        Math.abs(oxDelta) <= 0.75 &&
        Math.abs(oyDelta) <= 0.75;
      if (settled) {
        view.scale = target;
        view.ox = targetOx;
        view.oy = targetOy;
        return false;
      }
      view.scale += scaleDelta * 0.08;
      view.ox += oxDelta * 0.08;
      view.oy += oyDelta * 0.08;
      return true;
    }

    function resize() {
      if (!canvas) return;
      const rect = canvas.parentElement?.getBoundingClientRect();
      if (!rect) return;
      width = rect.width;
      height = rect.height;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      context!.setTransform(dpr, 0, 0, dpr, 0, 0);
      alpha = Math.max(alpha, 0.3);
      requestRender();
    }

    function step() {
      const repulsion = 9500;
      const springLength = 170;
      const springK = 0.03;
      const centerK = 0.006;
      const damping = 0.85;
      const minDistance = 56;

      for (let i = 0; i < nodes.length; i += 1) {
        const a = nodes[i];
        for (let j = i + 1; j < nodes.length; j += 1) {
          const b = nodes[j];
          let dx = a.x - b.x;
          let dy = a.y - b.y;
          let distSq = dx * dx + dy * dy;
          if (distSq < 1) {
            dx = (Math.sin(i * 7 + j) || 0.1) * 0.5;
            dy = (Math.cos(i * 3 + j) || 0.1) * 0.5;
            distSq = 0.25;
          }
          const dist = Math.sqrt(distSq);
          let force = (repulsion / distSq) * alpha;
          if (dist < minDistance) {
            force += (minDistance - dist) * 0.6;
          }
          const fx = (dx / dist) * force;
          const fy = (dy / dist) * force;
          a.vx += fx;
          a.vy += fy;
          b.vx -= fx;
          b.vy -= fy;
        }
      }

      for (const edge of edges) {
        const a = nodes[edge.a];
        const b = nodes[edge.b];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const dist = Math.max(1, Math.hypot(dx, dy));
        const force = (dist - springLength) * springK * alpha;
        const fx = (dx / dist) * force;
        const fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }

      for (const node of nodes) {
        node.vx += (width / 2 - node.x) * centerK * alpha;
        node.vy += (height / 2 - node.y) * centerK * alpha;
        if (node === dragged) {
          node.vx = 0;
          node.vy = 0;
          continue;
        }
        node.vx *= damping;
        node.vy *= damping;
        node.x += node.vx;
        node.y += node.vy;
      }

      alpha = Math.max(0.002, alpha * 0.985);
    }

    function radius(node: PhysicsNode) {
      return node.unresolved ? 3 : 4 + Math.min(6, node.degree * 1.2);
    }

    function draw() {
      const textNormal = cssVar("--text-normal") || "#1c1c1c";
      const textMuted = cssVar("--text-muted") || "#6b6b6b";
      const textFaint = cssVar("--text-faint") || "#9b9b9b";
      const border = cssVar("--background-modifier-border") || "#dcdcdc";
      const accentPrimary = cssVar("--accent-primary") || textNormal;

      const viewAnimating = fitView();
      context!.clearRect(0, 0, width, height);

      focusedNode = findNode(focusedIdRef.current);
      const spotlight = hovered ?? focusedNode;
      const neighborhood = new Set<number>();
      if (spotlight) {
        const spotlightIndex = nodes.indexOf(spotlight);
        neighborhood.add(spotlightIndex);
        for (const edge of edges) {
          if (edge.a === spotlightIndex) neighborhood.add(edge.b);
          if (edge.b === spotlightIndex) neighborhood.add(edge.a);
        }
      }

      for (const edge of edges) {
        const a = nodes[edge.a];
        const b = nodes[edge.b];
        const active =
          !spotlight || (neighborhood.has(edge.a) && neighborhood.has(edge.b));
        context!.strokeStyle = active ? textFaint : border;
        context!.globalAlpha = spotlight && !active ? 0.25 : 0.6;
        context!.lineWidth = 1;
        context!.beginPath();
        context!.moveTo(toScreenX(a.x), toScreenY(a.y));
        context!.lineTo(toScreenX(b.x), toScreenY(b.y));
        context!.stroke();
      }
      context!.globalAlpha = 1;

      nodes.forEach((node, index) => {
        const active = !spotlight || neighborhood.has(index);
        const r = radius(node);
        const sx = toScreenX(node.x);
        const sy = toScreenY(node.y);

        if (node === focusedNode) {
          context!.beginPath();
          context!.arc(sx, sy, r + 5, 0, Math.PI * 2);
          context!.strokeStyle = accentPrimary;
          context!.lineWidth = 1.5;
          context!.globalAlpha = 0.9;
          context!.stroke();
        }

        context!.beginPath();
        context!.arc(sx, sy, r, 0, Math.PI * 2);
        if (node.unresolved) {
          context!.fillStyle = border;
        } else {
          context!.fillStyle = active ? textNormal : textFaint;
        }
        context!.globalAlpha = active ? 1 : 0.35;
        context!.fill();

        context!.font =
          '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
        context!.fillStyle = node === hovered || node === focusedNode ? textNormal : textMuted;
        context!.textAlign = "center";
        context!.globalAlpha = active ? (node === hovered || node === focusedNode ? 1 : 0.85) : 0.25;
        context!.fillText(node.label, sx, sy + r + 12);
        context!.globalAlpha = 1;
      });

      return viewAnimating;
    }

    function stopLoop() {
      if (raf) cancelAnimationFrame(raf);
      raf = 0;
      running = false;
    }

    function scheduleFrame() {
      if (disposed || hidden || running) return;
      running = true;
      raf = requestAnimationFrame(loop);
    }

    function requestRender() {
      scheduleFrame();
    }

    function loop() {
      running = false;
      raf = 0;
      if (disposed || hidden) return;
      if (trackRaf) {
        metricsWindow.__wikiGraphRafCount = (metricsWindow.__wikiGraphRafCount ?? 0) + 1;
      }
      if (alpha > settleAlpha || dragged) step();
      const viewAnimating = draw();
      if (dragged || alpha > settleAlpha || viewAnimating) {
        scheduleFrame();
      }
    }

    function nodeAt(px: number, py: number): PhysicsNode | null {
      const { x, y } = toWorld(px, py);
      const slop = (4 + 4) / view.scale;
      for (let i = nodes.length - 1; i >= 0; i -= 1) {
        const node = nodes[i];
        if (Math.hypot(node.x - x, node.y - y) <= radius(node) / view.scale + slop) {
          return node;
        }
      }
      return null;
    }

    function pointer(event: PointerEvent) {
      const rect = canvas!.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function onPointerMove(event: PointerEvent) {
      const { x, y } = pointer(event);
      if (dragged) {
        dragTravel = Math.max(dragTravel, Math.hypot(x - dragOrigin.x, y - dragOrigin.y));
        const world = toWorld(x, y);
        dragged.x = world.x;
        dragged.y = world.y;
        alpha = Math.max(alpha, 0.25);
        requestRender();
        return;
      }
      const next = nodeAt(x, y);
      if (next !== hovered) {
        hovered = next;
        canvas!.style.cursor = next ? "pointer" : "default";
      }
      requestRender();
    }

    function onPointerDown(event: PointerEvent) {
      const { x, y } = pointer(event);
      const node = nodeAt(x, y);
      if (node) {
        dragged = node;
        dragOrigin = { x, y };
        dragTravel = 0;
        canvas!.setPointerCapture(event.pointerId);
        alpha = Math.max(alpha, 0.25);
        focusHandleRef.current?.(node.id);
        requestRender();
      }
    }

    function onPointerUp() {
      if (dragged) {
        const node = dragged;
        dragged = null;
        alpha = Math.max(alpha, 0.2);
        if (!node.unresolved && dragTravel < 4) {
          openNoteRef.current?.(node.id);
        }
      }
      requestRender();
    }

    function onPointerLeave() {
      if (hovered) {
        hovered = null;
        canvas!.style.cursor = "default";
        requestRender();
      }
    }

    function onVisibilityChange() {
      hidden = document.visibilityState === "hidden";
      if (hidden) {
        stopLoop();
        return;
      }
      requestRender();
    }

    function handleKeyboard(key: string) {
      if (nodes.length === 0) return;
      const ids = orderedIdsRef.current;
      const currentId = focusedIdRef.current;
      let index = currentId ? ids.indexOf(currentId) : -1;

      if (key === "ArrowRight" || key === "ArrowDown" || key === "j") {
        index = index < 0 ? 0 : (index + 1) % ids.length;
        const nextId = ids[index];
        focusHandleRef.current?.(nextId);
        const target = findNode(nextId);
        if (target) {
          alpha = Math.max(alpha, 0.15);
          requestRender();
        }
        return;
      }
      if (key === "ArrowLeft" || key === "ArrowUp" || key === "k") {
        index = index <= 0 ? ids.length - 1 : index - 1;
        const nextId = ids[index];
        focusHandleRef.current?.(nextId);
        const target = findNode(nextId);
        if (target) {
          alpha = Math.max(alpha, 0.15);
          requestRender();
        }
        return;
      }
      if (key === "Home") {
        const nextId = ids[0];
        if (nextId) focusHandleRef.current?.(nextId);
        requestRender();
        return;
      }
      if (key === "End") {
        const nextId = ids[ids.length - 1];
        if (nextId) focusHandleRef.current?.(nextId);
        requestRender();
        return;
      }
      if (key === "Enter" || key === " ") {
        const node = findNode(focusedIdRef.current);
        if (node && !node.unresolved) openNoteRef.current?.(node.id);
      }
    }

    focusHandleRef.current = (id) => onFocus(id);
    keyboardHandleRef.current = handleKeyboard;

    resize();

    const ids = Object.keys(links);
    const index = new Map<string, number>();
    nodes = ids.map((id, i) => {
      index.set(id, i);
      const angle = (i / ids.length) * Math.PI * 2;
      return {
        id,
        label: id.split("/").pop()?.replace(/\.md$/, "") ?? id,
        unresolved: false,
        degree: 0,
        x: width / 2 + Math.cos(angle) * 120,
        y: height / 2 + Math.sin(angle) * 120,
        vx: 0,
        vy: 0,
      };
    });

    for (const [source, entry] of Object.entries(links)) {
      const a = index.get(source);
      if (a === undefined) continue;
      for (const target of entry.outgoing) {
        const b = index.get(target);
        if (b === undefined) continue;
        edges.push({ a, b });
        nodes[a].degree += 1;
        nodes[b].degree += 1;
      }
      for (const ghost of entry.unresolved) {
        let g = index.get(`unresolved:${ghost}`);
        if (g === undefined) {
          g = nodes.length;
          index.set(`unresolved:${ghost}`, g);
          nodes.push({
            id: `unresolved:${ghost}`,
            label: ghost,
            unresolved: true,
            degree: 0,
            x: width / 2 + (Math.sin(g * 5) || 0.3) * 200,
            y: height / 2 + (Math.cos(g * 3) || 0.3) * 200,
            vx: 0,
            vy: 0,
          });
        }
        edges.push({ a, b: g });
      }
    }

    alpha = 1;
    requestRender();

    const observer = new ResizeObserver(resize);
    if (canvas.parentElement) observer.observe(canvas.parentElement);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointerleave", onPointerLeave);
    document.addEventListener("visibilitychange", onVisibilityChange);
    requestRender();

    return () => {
      disposed = true;
      stopLoop();
      observer.disconnect();
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointerleave", onPointerLeave);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      focusHandleRef.current = null;
      keyboardHandleRef.current = null;
    };
  }, [links, onFocus]);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
    const handler = keyboardHandleRef.current;
    if (!handler) return;
    const key = event.key;
    if (
      key === "ArrowLeft" ||
      key === "ArrowRight" ||
      key === "ArrowUp" ||
      key === "ArrowDown" ||
      key === "Home" ||
      key === "End" ||
      key === "Enter" ||
      key === " " ||
      key === "j" ||
      key === "k"
    ) {
      event.preventDefault();
      handler(key);
    }
  }

  return (
    <div className="graph-view-shell">
      <div
        ref={containerRef}
        className="graph-view"
        role="application"
        aria-label="Note link graph. Use arrow keys to move between nodes, Enter to open."
        tabIndex={0}
        onFocus={() => setInstructionsShown(true)}
        onKeyDown={onKeyDown}
      >
        <canvas ref={canvasRef} aria-hidden="true" />
        <div className={`graph-keyboard-hint${instructionsShown ? " is-visible" : ""}`}>
          Arrows or J / K move · Enter opens · switch to List for a flat, keyboard-first view.
        </div>
      </div>
    </div>
  );
}
