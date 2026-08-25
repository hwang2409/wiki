import { useEffect, useMemo, useRef, useState } from "react";
import type { NoteLinks } from "./api";

export type LinksMap = Record<string, NoteLinks>;
export type GraphCanvasControls = {
  zoomIn: () => void;
  zoomOut: () => void;
  fit: () => void;
};

export type GraphNodeSummary = {
  id: string;
  label: string;
  unresolved: boolean;
  degree: number;
  outgoing: string[];
  incoming: string[];
};

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

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function GraphCanvas({
  links,
  summaries,
  dense,
  focusedId,
  onFocus,
  onOpenNote,
  controlsRef,
}: {
  links: LinksMap;
  summaries: GraphNodeSummary[];
  dense: boolean;
  focusedId: string | null;
  onFocus: (id: string | null) => void;
  onOpenNote: (path: string) => void;
  controlsRef: { current: GraphCanvasControls | null };
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const focusedIdRef = useRef<string | null>(focusedId);
  const openNoteRef = useRef(onOpenNote);
  const focusHandleRef = useRef<((id: string | null) => void) | null>(null);
  const canvasKeyboardRef = useRef<((key: string) => void) | null>(null);
  const keyboardFocusRef = useRef(false);
  const orderedIdsRef = useRef<string[]>([]);
  const [instructionsShown, setInstructionsShown] = useState(false);

  // Keyboard navigation is authoritative on `summaries` (not the physics
  // node list) so it works even when the canvas 2d context is missing —
  // jsdom / older embeddings never install one and the physics-scoped
  // handler would otherwise never register.
  const orderedIds = useMemo(
    () =>
      [...summaries]
        .sort((a, b) => b.degree - a.degree || a.label.localeCompare(b.label))
        .map((node) => node.id),
    [summaries],
  );

  useEffect(() => {
    orderedIdsRef.current = orderedIds;
  }, [orderedIds]);

  const unresolvedById = useMemo(() => {
    const map = new Map<string, boolean>();
    for (const node of summaries) map.set(node.id, node.unresolved);
    return map;
  }, [summaries]);

  function handleKeyboardNav(key: string) {
    if (orderedIds.length === 0) return;
    const currentId = focusedIdRef.current;
    let index = currentId ? orderedIds.indexOf(currentId) : -1;
    const moveFocus = (id: string) => {
      keyboardFocusRef.current = true;
      focusedIdRef.current = id;
      onFocus(id);
      canvasKeyboardRef.current?.(key);
    };
    if (key === "ArrowRight" || key === "ArrowDown" || key === "j") {
      index = index < 0 ? 0 : (index + 1) % orderedIds.length;
      moveFocus(orderedIds[index]);
      return;
    }
    if (key === "ArrowLeft" || key === "ArrowUp" || key === "k") {
      index = index <= 0 ? orderedIds.length - 1 : index - 1;
      moveFocus(orderedIds[index]);
      return;
    }
    if (key === "Home") {
      const firstId = orderedIds[0];
      if (firstId) moveFocus(firstId);
      return;
    }
    if (key === "End") {
      const lastId = orderedIds[orderedIds.length - 1];
      if (lastId) moveFocus(lastId);
      return;
    }
    if (key === "Enter" || key === " ") {
      const activeId = focusedIdRef.current;
      if (!activeId) return;
      if (unresolvedById.get(activeId)) return;
      onOpenNote(activeId);
    }
  }

  useEffect(() => {
    focusedIdRef.current = focusedId;
  }, [focusedId]);

  useEffect(() => {
    openNoteRef.current = onOpenNote;
  }, [onOpenNote]);

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
    const minScale = 0.35;
    const maxScale = 3.5;
    let autoFit = true;
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
      const clampedTarget = Math.max(minScale, target);
      const targetOx = (width - (minX + maxX) * clampedTarget) / 2;
      const targetOy = (height - (minY + maxY) * clampedTarget) / 2;
      const scaleDelta = clampedTarget - view.scale;
      const oxDelta = targetOx - view.ox;
      const oyDelta = targetOy - view.oy;
      const settled =
        Math.abs(scaleDelta) <= 0.002 &&
        Math.abs(oxDelta) <= 0.75 &&
        Math.abs(oyDelta) <= 0.75;
      if (settled) {
        view.scale = clampedTarget;
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

    function revealFocusedNode() {
      const node = findNode(focusedIdRef.current);
      if (!node) return;
      const margin = 56;
      const sx = toScreenX(node.x);
      const sy = toScreenY(node.y);
      if (sx < margin) view.ox += margin - sx;
      if (sx > width - margin) view.ox -= sx - (width - margin);
      if (sy < margin) view.oy += margin - sy;
      if (sy > height - margin) view.oy -= sy - (height - margin);
    }

    function zoomAt(factor: number, px = width / 2, py = height / 2) {
      const world = toWorld(px, py);
      const nextScale = Math.max(minScale, Math.min(maxScale, view.scale * factor));
      view.scale = nextScale;
      view.ox = px - world.x * nextScale;
      view.oy = py - world.y * nextScale;
      autoFit = false;
      revealFocusedNode();
      requestRender();
    }

    function draw() {
      const textNormal = cssVar("--text-normal") || "#1c1c1c";
      const textMuted = cssVar("--text-muted") || "#6b6b6b";
      const textFaint = cssVar("--text-faint") || "#9b9b9b";
      const border = cssVar("--background-modifier-border") || "#dcdcdc";
      const accentPrimary = cssVar("--accent-primary") || textNormal;

      const viewAnimating = autoFit ? fitView() : false;
      if (autoFit && !viewAnimating && alpha <= settleAlpha) autoFit = false;
      context!.clearRect(0, 0, width, height);

      focusedNode = findNode(focusedIdRef.current);
      const spotlight = keyboardFocusRef.current ? focusedNode : hovered ?? focusedNode;
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

        const showLabel = !dense || Boolean(spotlight && active);
        if (showLabel) {
          context!.font =
            '10px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
          context!.fillStyle = node === hovered || node === focusedNode ? textNormal : textMuted;
          context!.textAlign = "center";
          context!.globalAlpha = active ? (node === hovered || node === focusedNode ? 1 : 0.85) : 0.25;
          context!.fillText(node.label, sx, sy + r + 12);
        }
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

    function pointer(event: { clientX: number; clientY: number }) {
      const rect = canvas!.getBoundingClientRect();
      return { x: event.clientX - rect.left, y: event.clientY - rect.top };
    }

    function onPointerMove(event: PointerEvent) {
      keyboardFocusRef.current = false;
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
      keyboardFocusRef.current = false;
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

    function onWheel(event: WheelEvent) {
      event.preventDefault();
      const { x, y } = pointer(event);
      zoomAt(event.deltaY < 0 ? 1.1 : 0.9, x, y);
    }

    function onVisibilityChange() {
      hidden = document.visibilityState === "hidden";
      if (hidden) {
        stopLoop();
        return;
      }
      requestRender();
    }

    // The container-level keyboard handler updates focusedId; here we only
    // nudge the physics alpha so the selection ring redraws.
    canvasKeyboardRef.current = () => {
      autoFit = false;
      revealFocusedNode();
      alpha = Math.max(alpha, 0.15);
      requestRender();
    };
    focusHandleRef.current = (id) => {
      focusedIdRef.current = id;
      autoFit = false;
      onFocus(id);
      revealFocusedNode();
      requestRender();
    };

    controlsRef.current = {
      zoomIn: () => zoomAt(1.2),
      zoomOut: () => zoomAt(0.84),
      fit: () => {
        autoFit = true;
        alpha = Math.max(alpha, 0.15);
        requestRender();
      },
    };

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
    canvas.addEventListener("wheel", onWheel, { passive: false });
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
      canvas.removeEventListener("wheel", onWheel);
      document.removeEventListener("visibilitychange", onVisibilityChange);
      focusHandleRef.current = null;
      canvasKeyboardRef.current = null;
      controlsRef.current = null;
    };
  }, [controlsRef, dense, links, onFocus]);

  function onKeyDown(event: React.KeyboardEvent<HTMLDivElement>) {
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
      handleKeyboardNav(key);
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
        <canvas
          ref={canvasRef}
          className="graph-canvas"
          data-graph-density={dense ? "dense" : "sparse"}
          aria-hidden="true"
        />
        <div className={`graph-keyboard-hint${instructionsShown ? " is-visible" : ""}`}>
          Arrows or J / K move · Enter opens · switch to List for a flat, keyboard-first view.
        </div>
      </div>
    </div>
  );
}
