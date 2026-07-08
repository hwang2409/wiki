import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import {
  AlertCircle,
  BookOpen,
  Bot,
  ChevronRight,
  ChevronsDownUp,
  FilePlus2,
  Folder as FolderIcon,
  HeartPulse,
  History,
  Moon,
  Pencil,
  Search,
  Settings,
  SquarePen,
  SquareTerminal,
  Sun,
  Waypoints,
  X
} from "lucide-react";
import {
  createNote,
  deleteNote,
  getAgents,
  getLinks,
  getNote,
  listNotes,
  renameNote,
  searchNotes,
  updateNote
} from "./api";
import type { AgentWorker, ArchivedWorker, NoteLinks, Orchestrator } from "./api";
import { FleetSwitcher, QuickSwitcher } from "./switcher";
import { SettingsModal, applyStoredMonoFont } from "./settings";
import { ActivityFeed } from "./activity";
import { AgentsSidebar, AgentsView, type AccountEvent } from "./agents";
import {
  AgentSessionView,
  type AgentRoutePanel,
  type AgentSessionSurfaceWorker,
} from "./agent-session-surface";
import { LoadingPlaceholder } from "./loading";
import { GraphView } from "./graph";
import { HealthView } from "./health";
import { KanbanBoard, appendDoneEntry } from "./kanban";
import { SecondaryPane } from "./pane";
import {
  ObsidianMarkdown,
  prepareMarkdown,
  splitFrontmatter,
  stripLeadingTitle
} from "./markdown";
import {
  applyTheme,
  getStoredTheme,
  getTheme,
  isDarkTheme,
  toggleThemePolarity,
  type ThemeId
} from "./themes";
import type { Note, NoteDraft, NoteSummary } from "./types";

type Mode = "empty" | "view" | "edit" | "new" | "activity" | "graph" | "health" | "agents" | "agent";
type UtilityMode = "activity" | "graph" | "health" | "agents";
type SidebarTab = "files" | "search" | "agents";
type SplitPosition = "left" | "right" | "top" | "bottom";
type DropZone = SplitPosition | "center";

type Layout =
  | { kind: "primary" }
  | { kind: "note"; id: string; path: string }
  | { kind: "split"; direction: "row" | "column"; ratio: number; first: Layout; second: Layout };

type PaneInfo = {
  key: string;
  kind: "primary" | "note";
  path: string | null;
};

type Direction = "left" | "right" | "up" | "down";
type FleetChooserMode = "orchestrators" | "tree";

type AgentsSnapshot = {
  workers: AgentWorker[] | null;
  orchestrators: Orchestrator[];
  archived: ArchivedWorker[];
  error: string | null;
};

type FleetItem = {
  kind: "orchestrator" | "worker";
  ticket: string;
  label: string;
  groupId: string;
  state: string | null;
  live: boolean;
  detail: string | null;
};

type FleetGroup = {
  orch: Orchestrator;
  items: FleetItem[];
  entryTicket: string;
  chooserEligible: boolean;
  meta: string;
};

function splitLayout(
  node: Layout,
  targetKey: string,
  position: SplitPosition,
  newPane: Layout
): Layout {
  const key = node.kind === "primary" ? "primary" : node.kind === "note" ? node.id : null;
  if (key === targetKey) {
    const direction = position === "left" || position === "right" ? "row" : "column";
    const newFirst = position === "left" || position === "top";
    return {
      kind: "split",
      direction,
      ratio: 0.5,
      first: newFirst ? newPane : node,
      second: newFirst ? node : newPane
    };
  }
  if (node.kind === "split") {
    return {
      ...node,
      first: splitLayout(node.first, targetKey, position, newPane),
      second: splitLayout(node.second, targetKey, position, newPane)
    };
  }
  return node;
}

function replaceNotePane(node: Layout, id: string, path: string): Layout {
  if (node.kind === "note" && node.id === id) return { ...node, path };
  if (node.kind === "split") {
    return {
      ...node,
      first: replaceNotePane(node.first, id, path),
      second: replaceNotePane(node.second, id, path)
    };
  }
  return node;
}

function setSplitRatio(node: Layout, path: number[], ratio: number): Layout {
  if (node.kind !== "split") return node;
  if (path.length === 0) return { ...node, ratio };
  const [head, ...rest] = path;
  return head === 1
    ? { ...node, first: setSplitRatio(node.first, rest, ratio) }
    : { ...node, second: setSplitRatio(node.second, rest, ratio) };
}

function closeNotePane(node: Layout, id: string): Layout | null {
  if (node.kind === "note" && node.id === id) return null;
  if (node.kind === "split") {
    const first = closeNotePane(node.first, id);
    const second = closeNotePane(node.second, id);
    if (first === null) return second;
    if (second === null) return first;
    return { ...node, first, second };
  }
  return node;
}

function layoutContains(node: Layout, key: string): boolean {
  if (node.kind === "primary") return key === "primary";
  if (node.kind === "note") return node.id === key;
  return layoutContains(node.first, key) || layoutContains(node.second, key);
}

function collectPaneInfos(node: Layout, panes: PaneInfo[] = []): PaneInfo[] {
  if (node.kind === "primary") {
    panes.push({ key: "primary", kind: "primary", path: null });
    return panes;
  }
  if (node.kind === "note") {
    panes.push({ key: node.id, kind: "note", path: node.path });
    return panes;
  }
  collectPaneInfos(node.first, panes);
  collectPaneInfos(node.second, panes);
  return panes;
}

function firstPaneKey(node: Layout): string {
  if (node.kind === "primary") return "primary";
  if (node.kind === "note") return node.id;
  return firstPaneKey(node.first);
}

function ticketFromPanePath(path: string | null): string | null {
  return path?.startsWith("agent://") ? path.slice("agent://".length) : null;
}

function cwdBasename(path: string | null): string {
  return path ? path.split("/").slice(-1)[0] : "no cwd";
}

function buildAgentSessionWorker(
  worker: Pick<AgentWorker, "ticket" | "kind" | "role" | "model" | "pr">
): AgentSessionSurfaceWorker {
  return {
    ticket: worker.ticket,
    kind: worker.kind,
    role: worker.role,
    model: worker.model,
    pr: worker.pr,
    canReview: Boolean(worker.pr),
  };
}

function buildFleetGroups(workers: AgentWorker[], orchestrators: Orchestrator[]): FleetGroup[] {
  const groups = orchestrators.map((orch): FleetGroup => {
    const owned = workers.filter((worker) => worker.orch === orch.id);
    return {
      orch,
      items: [
        {
          kind: "orchestrator" as const,
          ticket: orch.id,
          label: orch.id,
          groupId: orch.id,
          state: orch.window_alive ? "working" : "blocked",
          live: orch.window_alive,
          detail: cwdBasename(orch.cwd),
        },
        ...owned.map((worker) => ({
          kind: "worker" as const,
          ticket: worker.ticket,
          label: worker.ticket,
          groupId: orch.id,
          state: worker.state,
          live: worker.window_alive,
          detail: worker.step ?? worker.role ?? worker.kind,
        })),
      ],
      entryTicket: orch.id,
      chooserEligible: true,
      meta: `${cwdBasename(orch.cwd)} · ${orch.window_alive ? "live" : "dead"} · ${
        owned.length
      } worker${owned.length === 1 ? "" : "s"}`,
    };
  });

  const unattached = workers.filter(
    (worker) => !worker.orch || !orchestrators.some((orch) => orch.id === worker.orch)
  );
  if (unattached.length > 0) {
    groups.push({
      orch: {
        id: "unattached",
        window: null,
        window_alive: false,
        cwd: null,
        spawned_at: null,
        transcript_exists: false,
      },
      items: unattached.map((worker) => ({
        kind: "worker" as const,
        ticket: worker.ticket,
        label: worker.ticket,
        groupId: "unattached",
        state: worker.state,
        live: worker.window_alive,
        detail: worker.step ?? worker.role ?? worker.kind,
      })),
      entryTicket: unattached[0].ticket,
      chooserEligible: false,
      meta: `${unattached.length} unattached worker${unattached.length === 1 ? "" : "s"}`,
    });
  }

  return groups;
}

function findFleetGroup(groups: FleetGroup[], ticket: string | null): FleetGroup | null {
  if (!ticket) return null;
  return groups.find((group) => group.items.some((item) => item.ticket === ticket)) ?? null;
}

function findFleetItem(groups: FleetGroup[], ticket: string | null): FleetItem | null {
  if (!ticket) return null;
  for (const group of groups) {
    const match = group.items.find((item) => item.ticket === ticket);
    if (match) return match;
  }
  return null;
}

function agentStateGlyph(state: string | null, live: boolean): string {
  if (!live) return "○";
  if (state === "merge-ready") return "◎";
  if (state === "blocked") return "○";
  if (state === "working") return "●";
  return "·";
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (
    target instanceof HTMLInputElement ||
    target instanceof HTMLTextAreaElement ||
    target instanceof HTMLSelectElement
  ) {
    return true;
  }
  return target.isContentEditable || target.closest("[contenteditable='true']") !== null;
}

function rectCrossDistance(current: DOMRect, candidate: DOMRect, direction: Direction): number {
  if (direction === "left" || direction === "right") {
    const overlap = Math.min(current.bottom, candidate.bottom) - Math.max(current.top, candidate.top);
    if (overlap > 0) return 0;
    const currentMid = current.top + current.height / 2;
    const candidateMid = candidate.top + candidate.height / 2;
    return Math.abs(currentMid - candidateMid);
  }
  const overlap = Math.min(current.right, candidate.right) - Math.max(current.left, candidate.left);
  if (overlap > 0) return 0;
  const currentMid = current.left + current.width / 2;
  const candidateMid = candidate.left + candidate.width / 2;
  return Math.abs(currentMid - candidateMid);
}

function pickNeighborPane(
  panes: Map<string, DOMRect>,
  fromKey: string,
  direction: Direction
): string | null {
  const current = panes.get(fromKey);
  if (!current) return null;

  let bestKey: string | null = null;
  let bestRank: [number, number, number] | null = null;

  for (const [key, rect] of panes) {
    if (key === fromKey || rect.width === 0 || rect.height === 0) continue;

    let valid = false;
    let primaryGap = 0;
    if (direction === "left") {
      valid = rect.left < current.left - 4;
      primaryGap = Math.max(0, current.left - rect.right);
    } else if (direction === "right") {
      valid = rect.right > current.right + 4;
      primaryGap = Math.max(0, rect.left - current.right);
    } else if (direction === "up") {
      valid = rect.top < current.top - 4;
      primaryGap = Math.max(0, current.top - rect.bottom);
    } else {
      valid = rect.bottom > current.bottom + 4;
      primaryGap = Math.max(0, rect.top - current.bottom);
    }
    if (!valid) continue;

    const crossDistance = rectCrossDistance(current, rect, direction);
    const rank: [number, number, number] = [
      crossDistance === 0 ? 0 : 1,
      primaryGap,
      crossDistance,
    ];
    if (
      !bestRank ||
      rank[0] < bestRank[0] ||
      (rank[0] === bestRank[0] && rank[1] < bestRank[1]) ||
      (rank[0] === bestRank[0] && rank[1] === bestRank[1] && rank[2] < bestRank[2])
    ) {
      bestRank = rank;
      bestKey = key;
    }
  }

  return bestKey;
}

type TreeFolder = {
  name: string;
  path: string;
  folders: TreeFolder[];
  notes: NoteSummary[];
};

const emptyDraft: NoteDraft = { title: "", path: "", content: "" };

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

type Route =
  | { kind: "empty" }
  | { kind: "new" }
  | { kind: UtilityMode }
  | { kind: "agent"; ticket: string; panel: AgentRoutePanel }
  | { kind: "note" | "edit"; path: string };

const UTILITY_ROUTES: readonly UtilityMode[] = ["activity", "graph", "health", "agents"];

function routeHash(route: Route): string {
  if (route.kind === "empty") return "#/";
  if (route.kind === "new") return "#/new";
  if (route.kind === "agent") {
    const base = `#/agent/${encodeURIComponent(route.ticket)}`;
    return route.panel === "review" ? `${base}/review` : base;
  }
  if ((UTILITY_ROUTES as readonly string[]).includes(route.kind)) return `#/${route.kind}`;
  const encoded = (route as { path: string }).path
    .split("/")
    .map(encodeURIComponent)
    .join("/");
  return `#/${route.kind}/${encoded}`;
}

function parseRoute(hash: string): Route {
  if (hash === "#/new") return { kind: "new" };
  const agent = hash.match(/^#\/agent\/([A-Za-z0-9-]+)(?:\/(review))?$/);
  if (agent) return { kind: "agent", ticket: agent[1], panel: agent[2] === "review" ? "review" : null };
  const utility = UTILITY_ROUTES.find((kind) => hash === `#/${kind}`);
  if (utility) return { kind: utility };
  const match = hash.match(/^#\/(note|edit)\/(.+)$/);
  if (match) {
    const path = match[2].split("/").map(decodeURIComponent).join("/");
    return { kind: match[1] as "note" | "edit", path };
  }
  return { kind: "empty" };
}

function notesFingerprint(notes: NoteSummary[]) {
  return notes.map((note) => `${note.id}@${note.updated_at}`).join("|");
}

function readStoredCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem("wiki-collapsed-folders");
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((p): p is string => typeof p === "string") : []);
  } catch {
    return new Set();
  }
}

function buildTree(notes: NoteSummary[]): TreeFolder {
  const root: TreeFolder = { name: "", path: "", folders: [], notes: [] };
  const folderIndex = new Map<string, TreeFolder>([["", root]]);

  for (const note of notes) {
    const parts = note.path.split("/");
    let current = root;

    for (const part of parts.slice(0, -1)) {
      const folderPath = current.path ? `${current.path}/${part}` : part;
      let next = folderIndex.get(folderPath);
      if (!next) {
        next = { name: part, path: folderPath, folders: [], notes: [] };
        folderIndex.set(folderPath, next);
        current.folders.push(next);
      }
      current = next;
    }

    current.notes.push(note);
  }

  const sortFolder = (folder: TreeFolder) => {
    folder.folders.sort((a, b) => a.name.localeCompare(b.name));
    folder.notes.sort((a, b) => basename(a.path).localeCompare(basename(b.path)));
    folder.folders.forEach(sortFolder);
  };
  sortFolder(root);

  return root;
}

function collectFolderPaths(folder: TreeFolder, paths: string[] = []): string[] {
  for (const child of folder.folders) {
    paths.push(child.path);
    collectFolderPaths(child, paths);
  }
  return paths;
}

function countWords(content: string) {
  const words = content.trim().split(/\s+/).filter(Boolean).length;
  return { words, characters: content.length };
}

const DROP_ZONES: readonly DropZone[] = ["left", "right", "top", "bottom", "center"];

type ContextMenuState = {
  x: number;
  y: number;
  kind: "file" | "folder";
  path: string;
};

type DialogState = {
  title: string;
  input?: string;
  confirmLabel: string;
  danger?: boolean;
  onConfirm: (value: string) => void;
};

function Dialog({ dialog, onClose }: { dialog: DialogState; onClose: () => void }) {
  const [value, setValue] = useState(dialog.input ?? "");

  function confirm() {
    onClose();
    dialog.onConfirm(value.trim());
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        aria-label={dialog.title}
        className="dialog"
        role="dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="dialog-title">{dialog.title}</div>
        {dialog.input !== undefined ? (
          <input
            autoFocus
            className="dialog-input"
            type="text"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                confirm();
              } else if (event.key === "Escape") {
                event.preventDefault();
                onClose();
              }
            }}
          />
        ) : null}
        <div className="dialog-actions">
          <button className="dialog-button" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            autoFocus={dialog.input === undefined}
            className={`dialog-button dialog-confirm${dialog.danger ? " is-danger" : ""}`}
            type="button"
            onClick={confirm}
          >
            {dialog.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

function PaneDivider({
  direction,
  onRatio
}: {
  direction: "row" | "column";
  onRatio: (ratio: number) => void;
}) {
  return (
    <div
      aria-orientation={direction === "row" ? "vertical" : "horizontal"}
      className={`pane-divider ${direction}`}
      role="separator"
      onPointerDown={(event) => {
        event.preventDefault();
        const divider = event.currentTarget;
        const parent = divider.parentElement;
        if (!parent) return;
        const rect = parent.getBoundingClientRect();
        divider.setPointerCapture(event.pointerId);
        divider.classList.add("is-dragging");
        document.body.classList.add("is-resizing-panes");

        const move = (moveEvent: PointerEvent) => {
          const raw =
            direction === "row"
              ? (moveEvent.clientX - rect.left) / rect.width
              : (moveEvent.clientY - rect.top) / rect.height;
          onRatio(Math.min(0.85, Math.max(0.15, raw)));
        };
        const up = () => {
          divider.classList.remove("is-dragging");
          document.body.classList.remove("is-resizing-panes");
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      }}
    />
  );
}

function PaneDropTarget({
  active,
  children,
  onDropZone
}: {
  active: boolean;
  children: ReactNode;
  onDropZone: (zone: DropZone) => void;
}) {
  const [hovered, setHovered] = useState<DropZone | null>(null);

  useEffect(() => {
    if (!active) setHovered(null);
  }, [active]);

  return (
    <div className="pane-wrap">
      {children}
      {active ? (
        <div className="pane-drop-overlay">
          {DROP_ZONES.map((zone) => (
            <div
              className={`pane-drop-zone pane-drop-${zone}${
                hovered === zone ? " is-active" : ""
              }`}
              key={zone}
              onDragLeave={() => setHovered((prev) => (prev === zone ? null : prev))}
              onDragOver={(event) => {
                event.preventDefault();
                event.dataTransfer.dropEffect = "copy";
                setHovered((prev) => (prev === zone ? prev : zone));
              }}
              onDrop={(event) => {
                event.preventDefault();
                setHovered(null);
                onDropZone(zone);
              }}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function FolderTree({
  folder,
  depth,
  activePath,
  collapsed,
  dragActive,
  onToggleFolder,
  onOpenNote,
  onNoteDragStart,
  onNoteDragEnd,
  onDropOnFolder,
  onContextMenu
}: {
  folder: TreeFolder;
  depth: number;
  activePath: string | null;
  collapsed: Set<string>;
  dragActive: boolean;
  onToggleFolder: (path: string) => void;
  onOpenNote: (path: string) => void;
  onNoteDragStart: (path: string) => void;
  onNoteDragEnd: () => void;
  onDropOnFolder: (folderPath: string) => void;
  onContextMenu: (event: ReactMouseEvent, kind: "file" | "folder", path: string) => void;
}) {
  return (
    <>
      {folder.folders.map((child) => {
        const isCollapsed = collapsed.has(child.path);
        return (
          <div className="tree-item" key={child.path}>
            <button
              className="tree-item-self nav-folder-title"
              style={{ paddingInlineStart: `${depth * 17 + 4}px` }}
              type="button"
              onClick={() => onToggleFolder(child.path)}
              onContextMenu={(event) => onContextMenu(event, "folder", child.path)}
              onDragLeave={(event) => {
                if (dragActive) event.currentTarget.classList.remove("is-drop-target");
              }}
              onDragOver={(event) => {
                if (!dragActive) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                event.currentTarget.classList.add("is-drop-target");
              }}
              onDrop={(event) => {
                if (!dragActive) return;
                event.preventDefault();
                event.currentTarget.classList.remove("is-drop-target");
                onDropOnFolder(child.path);
              }}
            >
              <ChevronRight
                className={`collapse-icon${isCollapsed ? " is-collapsed" : ""}`}
                size={16}
              />
              <span className="tree-item-name">{child.name}</span>
            </button>
            {isCollapsed ? null : (
              <FolderTree
                activePath={activePath}
                collapsed={collapsed}
                depth={depth + 1}
                dragActive={dragActive}
                folder={child}
                onContextMenu={onContextMenu}
                onDropOnFolder={onDropOnFolder}
                onNoteDragEnd={onNoteDragEnd}
                onNoteDragStart={onNoteDragStart}
                onOpenNote={onOpenNote}
                onToggleFolder={onToggleFolder}
              />
            )}
          </div>
        );
      })}
      {folder.notes.map((note) => (
        <button
          className={`tree-item-self nav-file-title${
            activePath === note.path ? " is-active" : ""
          }`}
          draggable
          key={note.id}
          style={{ paddingInlineStart: `${depth * 17 + 24}px` }}
          type="button"
          onClick={() => onOpenNote(note.path)}
          onContextMenu={(event) => onContextMenu(event, "file", note.path)}
          onDragEnd={onNoteDragEnd}
          onDragStart={(event) => {
            event.dataTransfer.effectAllowed = "copyMove";
            event.dataTransfer.setData("text/plain", note.path);
            onNoteDragStart(note.path);
          }}
        >
          <span className="tree-item-name">{basename(note.path)}</span>
        </button>
      ))}
    </>
  );
}

export default function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [activeNote, setActiveNote] = useState<Note | null>(null);
  const [mode, setMode] = useState<Mode>("empty");
  const [draft, setDraft] = useState<NoteDraft>(emptyDraft);
  const [query, setQuery] = useState("");
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>(() => {
    const stored = localStorage.getItem("wiki-sidebar-tab");
    return stored === "search" || stored === "agents" ? stored : "files";
  });
  const [agentTicket, setAgentTicket] = useState<string | null>(null);
  const [agentPanel, setAgentPanel] = useState<AgentRoutePanel>(null);
  const [leaderArmed, setLeaderArmed] = useState(false);
  const [fleetChooser, setFleetChooser] = useState<FleetChooserMode | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    applyStoredMonoFont();
  }, []);

  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(readStoredCollapsed);
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [sidebarVisible, setSidebarVisible] = useState(
    () => localStorage.getItem("wiki-sidebar-visible") !== "false"
  );
  const [draggingNotePath, setDraggingNotePath] = useState<string | null>(null);
  const [layout, setLayout] = useState<Layout>({ kind: "primary" });
  const [focusedPaneId, setFocusedPaneId] = useState("primary");
  const [zoomedPaneId, setZoomedPaneId] = useState<string | null>(null);
  const [activeGroupId, setActiveGroupId] = useState<string | null>(
    () => localStorage.getItem("wiki-active-orch-group")
  );
  const [links, setLinks] = useState<Record<string, NoteLinks>>({});
  const [agentsState, setAgentsState] = useState<AgentsSnapshot>({
    workers: null,
    orchestrators: [],
    archived: [],
    error: null,
  });
  const [refreshTick, setRefreshTick] = useState(0);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const paneIdRef = useRef(0);
  const paneRefs = useRef(new Map<string, HTMLDivElement>());
  const leaderTimerRef = useRef<number | null>(null);
  const lastSelectedAgentRef = useRef<string | null>(null);

  useEffect(() => {
    if (!contextMenu) return;
    const close = () => setContextMenu(null);
    window.addEventListener("click", close);
    window.addEventListener("contextmenu", close);
    window.addEventListener("blur", close);
    return () => {
      window.removeEventListener("click", close);
      window.removeEventListener("contextmenu", close);
      window.removeEventListener("blur", close);
    };
  }, [contextMenu]);

  const [accountEvents, setAccountEvents] = useState<AccountEvent[]>([]);
  useEffect(() => {
    const source = new EventSource("/api/events");
    source.onmessage = (raw) => {
      setRefreshTick((tick) => tick + 1);
      try {
        const payload = JSON.parse(raw.data) as { type?: string };
        if (
          payload &&
          typeof payload.type === "string" &&
          (payload.type === "codex_rotation" ||
            payload.type === "codex_limit_no_eligible" ||
            payload.type === "codex_rotation_failed" ||
            payload.type === "claude_limit_hit")
        ) {
          setAccountEvents((prior) => [payload as AccountEvent, ...prior].slice(0, 4));
        }
      } catch {
        /* ignore malformed frames */
      }
    };
    return () => source.close();
  }, []);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<ThemeId>(() => getStoredTheme());
  // Lifted out of AgentsView: pane splits remount the view, sidebar must survive.
  const [agentsOpenTicket, setAgentsOpenTicket] = useState<string | null>(null);
  const viewContentRef = useRef<HTMLDivElement | null>(null);

  const appliedHashRef = useRef<string | null>(null);

  useEffect(() => {
    applyTheme(theme);
  }, [theme]);

  useEffect(() => {
    localStorage.setItem("wiki-sidebar-tab", sidebarTab);
  }, [sidebarTab]);

  useEffect(() => {
    localStorage.setItem("wiki-sidebar-visible", String(sidebarVisible));
  }, [sidebarVisible]);

  useEffect(() => {
    if (activeGroupId) {
      localStorage.setItem("wiki-active-orch-group", activeGroupId);
    } else {
      localStorage.removeItem("wiki-active-orch-group");
    }
  }, [activeGroupId]);

  useEffect(() => {
    localStorage.setItem("wiki-collapsed-folders", JSON.stringify([...collapsedFolders]));
  }, [collapsedFolders]);

  useEffect(() => {
    let ignore = false;
    getAgents()
      .then((result) => {
        if (ignore) return;
        setAgentsState({
          workers: result.workers,
          orchestrators: result.orchestrators ?? [],
          archived: result.archived ?? [],
          error: null,
        });
      })
      .catch((err) => {
        if (ignore) return;
        const message = err instanceof Error ? err.message : "Could not load agents";
        setAgentsState((current) =>
          current.workers === null
            ? { workers: [], orchestrators: [], archived: [], error: message }
            : { ...current, error: message }
        );
      });
    return () => {
      ignore = true;
    };
  }, [refreshTick]);

  useEffect(
    () => () => {
      if (leaderTimerRef.current) window.clearTimeout(leaderTimerRef.current);
    },
    []
  );

  useEffect(() => {
    viewContentRef.current?.scrollTo(0, 0);
  }, [activeNote?.path, mode]);

  useEffect(() => {
    if (mode !== "view" || !activeNote) return;
    let ignore = false;
    getLinks()
      .then((next) => {
        if (!ignore) setLinks(next);
      })
      .catch(() => {});
    return () => {
      ignore = true;
    };
  }, [mode, activeNote?.path, activeNote?.updated_at, refreshTick]);

  useEffect(() => {
    let ignore = false;

    async function boot() {
      setIsLoading(true);
      setError(null);
      try {
        const nextNotes = await listNotes();
        if (ignore) return;
        setNotes(nextNotes);
      } catch (err) {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load notes");
      } finally {
        if (!ignore) setIsLoading(false);
      }
    }

    boot();
    return () => {
      ignore = true;
    };
  }, []);

  const tree = useMemo(() => buildTree(notes), [notes]);
  const paneInfos = useMemo(() => collectPaneInfos(layout), [layout]);
  const paneMap = useMemo(() => new Map(paneInfos.map((pane) => [pane.key, pane])), [paneInfos]);
  const agentWorkers = useMemo(() => {
    const map = new Map<string, AgentSessionSurfaceWorker>();
    for (const worker of agentsState.workers ?? []) {
      map.set(worker.ticket, buildAgentSessionWorker(worker));
    }
    for (const worker of agentsState.archived ?? []) {
      if (!map.has(worker.ticket)) map.set(worker.ticket, buildAgentSessionWorker(worker));
    }
    return map;
  }, [agentsState.archived, agentsState.workers]);
  const fleetGroups = useMemo(
    () => buildFleetGroups(agentsState.workers ?? [], agentsState.orchestrators),
    [agentsState.orchestrators, agentsState.workers]
  );
  const activeAgentWorker = agentTicket ? agentWorkers.get(agentTicket) ?? { ticket: agentTicket } : null;
  const primaryViewedAgentTicket = mode === "agent" ? agentTicket : mode === "agents" ? agentsOpenTicket : null;
  const focusedPaneTicket =
    focusedPaneId === "primary"
      ? primaryViewedAgentTicket
      : ticketFromPanePath(paneMap.get(focusedPaneId)?.path ?? null);
  const currentViewedAgentTicket = focusedPaneTicket ?? primaryViewedAgentTicket ?? null;
  const activeGroup =
    fleetGroups.find((group) => group.orch.id === activeGroupId) ?? fleetGroups[0] ?? null;
  const activeGroupTicket = useMemo(() => {
    if (!activeGroup) return null;
    const preferredTickets = [
      currentViewedAgentTicket,
      lastSelectedAgentRef.current,
      activeGroup.entryTicket,
    ];
    return (
      preferredTickets.find((ticket) =>
        activeGroup.items.some((item) => item.ticket === ticket)
      ) ?? activeGroup.entryTicket
    );
  }, [activeGroup, currentViewedAgentTicket]);
  const activeGroupIndex = activeGroup
    ? fleetGroups.findIndex((group) => group.orch.id === activeGroup.orch.id)
    : -1;
  const activeItemIndex =
    activeGroup && activeGroupTicket
      ? Math.max(0, activeGroup.items.findIndex((item) => item.ticket === activeGroupTicket))
      : 0;
  const modalOpen =
    switcherOpen || settingsOpen || fleetChooser !== null || dialog !== null || contextMenu !== null;
  const orchestratorChooserItems = useMemo(
    () =>
      fleetGroups.filter((group) => group.chooserEligible).map((group) => ({
        key: `orch:${group.orch.id}`,
        value: group.orch.id,
        icon: <Bot size={14} />,
        label: group.orch.id,
        meta: group.meta,
        active: activeGroup?.orch.id === group.orch.id,
      })),
    [activeGroup, fleetGroups]
  );
  const fleetTreeItems = useMemo(
    () =>
      fleetGroups.flatMap((group) => [
        {
          key: `tree-orch:${group.orch.id}`,
          value: group.entryTicket,
          icon: <Bot size={14} />,
          label: group.orch.id,
          meta: group.meta,
          active: activeGroupTicket === group.entryTicket,
        },
        ...group.items.slice(group.chooserEligible ? 1 : 0).map((item) => ({
          key: `tree-worker:${item.ticket}`,
          value: item.ticket,
          icon: <span className="fleet-switcher-glyph">{agentStateGlyph(item.state, item.live)}</span>,
          label: item.ticket,
          meta: `${item.state ?? "unknown"}${item.detail ? ` · ${item.detail}` : ""}`,
          indent: 1,
          active: activeGroupTicket === item.ticket,
        })),
      ]),
    [activeGroupTicket, fleetGroups]
  );

  useEffect(() => {
    if (!paneInfos.some((pane) => pane.key === focusedPaneId)) {
      const fallback = paneInfos[0]?.key ?? "primary";
      setFocusedPaneId(fallback);
      requestAnimationFrame(() => paneRefs.current.get(fallback)?.focus());
    }
  }, [focusedPaneId, paneInfos]);

  useEffect(() => {
    if (zoomedPaneId && !paneInfos.some((pane) => pane.key === zoomedPaneId)) {
      setZoomedPaneId(null);
    }
  }, [paneInfos, zoomedPaneId]);

  useEffect(() => {
    if (!fleetGroups.length) {
      setActiveGroupId(null);
      return;
    }
    if (!activeGroupId || !fleetGroups.some((group) => group.orch.id === activeGroupId)) {
      setActiveGroupId(fleetGroups[0].orch.id);
    }
  }, [activeGroupId, fleetGroups]);

  useEffect(() => {
    if (!currentViewedAgentTicket) return;
    lastSelectedAgentRef.current = currentViewedAgentTicket;
    const group = findFleetGroup(fleetGroups, currentViewedAgentTicket);
    if (group && group.orch.id !== activeGroupId) setActiveGroupId(group.orch.id);
  }, [activeGroupId, currentViewedAgentTicket, fleetGroups]);

  const [searchResults, setSearchResults] = useState<NoteSummary[]>([]);

  useEffect(() => {
    const needle = query.trim();
    if (!needle) {
      setSearchResults([]);
      return;
    }

    let ignore = false;
    const timer = window.setTimeout(async () => {
      try {
        const results = await searchNotes(needle);
        if (!ignore) setSearchResults(results);
      } catch {
        if (!ignore) setSearchResults([]);
      }
    }, 150);

    return () => {
      ignore = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  async function refreshNotes(selectPath?: string) {
    const nextNotes = await listNotes();
    setNotes(nextNotes);
    const targetPath = selectPath ?? activeNote?.path;
    if (targetPath) setActiveNote(await getNote(targetPath));
  }

  function navigate(route: Route) {
    const hash = routeHash(route);
    appliedHashRef.current = hash;
    if (window.location.hash !== hash) window.location.hash = hash;
  }

  async function openNote(path: string) {
    navigate({ kind: "note", path });
    setError(null);
    setAgentPanel(null);
    setMode("view");
    try {
      const note = await getNote(path);
      setActiveNote(note);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open note");
    }
  }

  function openUtilityView(kind: UtilityMode) {
    navigate({ kind });
    setError(null);
    setActiveNote(null);
    setAgentPanel(null);
    setMode(kind);
  }

  function openAgent(ticket: string, panel: AgentRoutePanel = null) {
    navigate({ kind: "agent", panel, ticket });
    setError(null);
    setActiveNote(null);
    setAgentPanel(panel);
    setAgentTicket(ticket);
    setMode("agent");
  }

  function registerPaneRef(key: string, node: HTMLDivElement | null) {
    if (node) paneRefs.current.set(key, node);
    else paneRefs.current.delete(key);
  }

  function focusPane(key: string) {
    setFocusedPaneId(key);
    requestAnimationFrame(() => paneRefs.current.get(key)?.focus());
  }

  function activePaneFrame(): HTMLDivElement | null {
    const active = document.activeElement;
    return active instanceof HTMLDivElement && active.classList.contains("pane-frame")
      ? active
      : null;
  }

  function paneScopeScroll(delta: number): boolean {
    const frame = activePaneFrame();
    if (!frame) return false;
    const scroller = frame.querySelector<HTMLDivElement>(".session-scroll");
    if (!scroller) return false;
    scroller.scrollBy({ top: delta });
    return true;
  }

  function paneScopeBottom(): boolean {
    const frame = activePaneFrame();
    if (!frame) return false;
    const scroller = frame.querySelector<HTMLDivElement>(".session-scroll");
    if (!scroller) return false;
    scroller.scrollTo({ top: scroller.scrollHeight });
    return true;
  }

  function paneScopeHalfPage(direction: 1 | -1): boolean {
    const frame = activePaneFrame();
    if (!frame) return false;
    const scroller = frame.querySelector<HTMLDivElement>(".session-scroll");
    if (!scroller) return false;
    scroller.scrollBy({ top: Math.round((scroller.clientHeight / 2) * direction) });
    return true;
  }

  function focusPaneComposer(): boolean {
    const frame = activePaneFrame();
    if (!frame) return false;
    const composer = frame.querySelector<HTMLTextAreaElement>(".session-composer textarea");
    if (!composer) return false;
    composer.focus();
    return true;
  }

  function handlePaneScopeKey(event: globalThis.KeyboardEvent): boolean {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    const key = event.key;
    const lowerKey = key.toLowerCase();
    if (lowerKey === "j") return paneScopeScroll(60);
    if (lowerKey === "k") return paneScopeScroll(-60);
    if (lowerKey === "d") return paneScopeHalfPage(1);
    if (lowerKey === "u") return paneScopeHalfPage(-1);
    if (lowerKey === "i") return focusPaneComposer();
    if (key === "G") return paneScopeBottom();
    return false;
  }

  function disarmLeader() {
    if (leaderTimerRef.current) {
      window.clearTimeout(leaderTimerRef.current);
      leaderTimerRef.current = null;
    }
    setLeaderArmed(false);
  }

  function armLeader() {
    if (leaderTimerRef.current) window.clearTimeout(leaderTimerRef.current);
    setLeaderArmed(true);
    leaderTimerRef.current = window.setTimeout(() => {
      leaderTimerRef.current = null;
      setLeaderArmed(false);
    }, 1500);
  }

  function openAgentInContext(ticket: string) {
    lastSelectedAgentRef.current = ticket;
    const group = findFleetGroup(fleetGroups, ticket);
    if (group && group.orch.id !== activeGroupId) setActiveGroupId(group.orch.id);

    const pane = paneMap.get(focusedPaneId);
    const targetPaneId =
      focusedPaneId !== "primary" && ticketFromPanePath(pane?.path ?? null)
        ? focusedPaneId
        : "primary";

    if (zoomedPaneId && zoomedPaneId !== targetPaneId) setZoomedPaneId(null);

    if (targetPaneId === "primary") {
      openAgent(ticket);
      focusPane("primary");
      return;
    }

    setLayout((current) => replaceNotePane(current, targetPaneId, `agent://${ticket}`));
    focusPane(targetPaneId);
  }

  function movePaneFocus(direction: Direction) {
    const rects = new Map(
      [...paneRefs.current.entries()].map(([key, element]) => [key, element.getBoundingClientRect()])
    );
    const nextPane = pickNeighborPane(rects, focusedPaneId, direction);
    if (nextPane) focusPane(nextPane);
  }

  function closeFocusedPane() {
    if (focusedPaneId === "primary") return;
    const target = focusedPaneId;
    if (zoomedPaneId === target) setZoomedPaneId(null);
    setLayout((current) => closeNotePane(current, target) ?? { kind: "primary" });
  }

  function startNewNote() {
    navigate({ kind: "new" });
    setError(null);
    setActiveNote(null);
    setDraft(emptyDraft);
    setMode("new");
  }

  function startEditing() {
    if (!activeNote) return;
    navigate({ kind: "edit", path: activeNote.path });
    setError(null);
    setDraft({
      title: activeNote.title,
      path: activeNote.path,
      content: activeNote.content
    });
    setMode("edit");
  }

  function cancelEditing() {
    navigate(activeNote ? { kind: "note", path: activeNote.path } : { kind: "empty" });
    setError(null);
    setDraft(emptyDraft);
    setMode(activeNote ? "view" : "empty");
  }

  function handleTreeContextMenu(
    event: ReactMouseEvent,
    kind: "file" | "folder",
    path: string
  ) {
    event.preventDefault();
    event.stopPropagation();
    setContextMenu({ x: event.clientX, y: event.clientY, kind, path });
  }

  async function doRename(oldPath: string, newPathRaw: string) {
    const newPath = newPathRaw.endsWith(".md") ? newPathRaw : `${newPathRaw}.md`;
    setError(null);
    try {
      const result = await renameNote(oldPath, newPath);
      setNotes(await listNotes());
      if (activeNote?.path === oldPath) openNote(result.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not rename note");
    }
  }

  function promptRename(path: string) {
    setDialog({
      title: "Rename / move note",
      input: path,
      confirmLabel: "Rename",
      onConfirm: (value) => {
        if (value && value !== path) doRename(path, value);
      }
    });
  }

  function promptDelete(path: string) {
    setDialog({
      title: `Delete ${basename(path)}? Git is the undo.`,
      confirmLabel: "Delete",
      danger: true,
      onConfirm: async () => {
        setError(null);
        try {
          await deleteNote(path);
          setNotes(await listNotes());
          if (activeNote?.path === path) {
            navigate({ kind: "empty" });
            setActiveNote(null);
            setMode("empty");
          }
        } catch (err) {
          setError(err instanceof Error ? err.message : "Could not delete note");
        }
      }
    });
  }

  async function createFromPath(rawPath: string) {
    const path = rawPath.endsWith(".md") ? rawPath : `${rawPath}.md`;
    const slug = basename(path);
    const title = slug.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
    const topic = path.includes("/") ? path.split("/")[0] : "meta";
    const today = new Intl.DateTimeFormat("en-CA").format(new Date());
    const content = `---\ntype: reference\ntags: [${topic}]\ncreated: ${today}\nupdated: ${today}\n---\n\n# ${title}\n`;
    setError(null);
    try {
      const created = await createNote({ title, path, content });
      setNotes(await listNotes());
      openNote(created.path);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not create note");
    }
  }

  function promptNewNoteIn(folderPath: string) {
    setDialog({
      title: "New note",
      input: `${folderPath}/`,
      confirmLabel: "Create",
      onConfirm: (value) => {
        if (value && !value.endsWith("/")) createFromPath(value);
      }
    });
  }

  function promptCreateUnresolved(target: string) {
    const topic = activeNote?.path.includes("/")
      ? activeNote.path.split("/")[0]
      : "meta";
    const slug = target
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "");
    setDialog({
      title: `Create note for [[${target}]]`,
      input: `${topic}/${slug}`,
      confirmLabel: "Create",
      onConfirm: (value) => {
        if (value) createFromPath(value);
      }
    });
  }

  function handleDropOnFolder(folderPath: string) {
    const source = draggingNotePath;
    setDraggingNotePath(null);
    if (!source) return;
    const dest = `${folderPath}/${basename(source)}.md`;
    if (dest !== source) doRename(source, dest);
  }

  async function saveKanbanContent(next: string) {
    if (!activeNote) return;
    setError(null);
    try {
      const updated = await updateNote(activeNote.path, next);
      setActiveNote(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save board change");
    }
  }

  async function completeKanbanCard(card: { start: number; end: number; text: string }) {
    if (!activeNote) return;
    setError(null);
    try {
      const date = new Intl.DateTimeFormat("en-CA").format(new Date());
      const summary = card.text.replace(/^\[P\d\]\s*/, "");
      const done = await getNote("log/done.md");
      await updateNote("log/done.md", appendDoneEntry(done.content, summary, date));

      const lines = activeNote.content.split("\n");
      lines.splice(card.start, card.end - card.start + 1);
      const updated = await updateNote(activeNote.path, lines.join("\n"));
      setActiveNote(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete card");
    }
  }

  async function saveDraft() {
    setError(null);
    setIsSaving(true);

    try {
      if (mode === "new") {
        const created = await createNote(draft);
        await refreshNotes(created.path);
        navigate({ kind: "note", path: created.path });
        setMode("view");
        return;
      }
      if (activeNote) {
        const updated = await updateNote(activeNote.path, draft.content);
        await refreshNotes(updated.path);
        navigate({ kind: "note", path: updated.path });
        setMode("view");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save note");
    } finally {
      setIsSaving(false);
    }
  }

  useEffect(() => {
    let disposed = false;

    async function fetchNoteWithRetry(path: string) {
      let lastError: unknown;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        try {
          return await getNote(path);
        } catch (err) {
          lastError = err;
          if (disposed) throw err;
          await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)));
        }
      }
      throw lastError;
    }

    async function applyRoute() {
      const hash = window.location.hash || "#/";
      if (appliedHashRef.current === hash) return;
      appliedHashRef.current = hash;
      const route = parseRoute(hash);

      setError(null);
      if (route.kind === "empty") {
        setActiveNote(null);
        setAgentPanel(null);
        setMode("empty");
        return;
      }
      if (route.kind === "new") {
        setActiveNote(null);
        setAgentPanel(null);
        setDraft(emptyDraft);
        setMode("new");
        return;
      }
      if (route.kind === "agent") {
        setActiveNote(null);
        setAgentPanel(route.panel);
        setAgentTicket(route.ticket);
        setMode("agent");
        return;
      }
      if (route.kind !== "note" && route.kind !== "edit") {
        setActiveNote(null);
        setAgentPanel(null);
        setMode(route.kind);
        return;
      }

      try {
        const note = await fetchNoteWithRetry(route.path);
        if (disposed) return;
        setActiveNote(note);
        if (route.kind === "edit") {
          setDraft({ title: note.title, path: note.path, content: note.content });
          setMode("edit");
        } else {
          setMode("view");
        }
      } catch (err) {
        if (disposed) return;
        appliedHashRef.current = null;
        setError(err instanceof Error ? err.message : "Could not open note");
        setActiveNote(null);
        setMode("empty");
      }
    }

    applyRoute();
    window.addEventListener("hashchange", applyRoute);
    return () => {
      disposed = true;
      appliedHashRef.current = null;
      window.removeEventListener("hashchange", applyRoute);
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.defaultPrevented) return;

      const key = event.key;
      const lowerKey = key.toLowerCase();

      if (leaderArmed) {
        if (isEditableTarget(event.target)) {
          disarmLeader();
          return;
        }
        if (["Shift", "Control", "Alt", "Meta"].includes(key)) return;
        event.preventDefault();
        disarmLeader();

        if (key === "Escape") return;

        if (activeGroup && (lowerKey === "n" || lowerKey === "p")) {
          const delta = lowerKey === "n" ? 1 : -1;
          const nextIndex =
            (activeItemIndex + delta + activeGroup.items.length) % activeGroup.items.length;
          openAgentInContext(activeGroup.items[nextIndex].ticket);
          return;
        }

        if (activeGroup && /^\d$/.test(key)) {
          const target = activeGroup.items[Number(key)];
          if (target) openAgentInContext(target.ticket);
          return;
        }

        if (fleetGroups.length > 0 && (key === "(" || key === ")")) {
          const delta = key === ")" ? 1 : -1;
          const nextIndex =
            activeGroupIndex >= 0
              ? (activeGroupIndex + delta + fleetGroups.length) % fleetGroups.length
              : 0;
          openAgentInContext(fleetGroups[nextIndex].entryTicket);
          return;
        }

        if (lowerKey === "s") {
          setFleetChooser("orchestrators");
          return;
        }

        if (lowerKey === "w") {
          setFleetChooser("tree");
          return;
        }

        if (lowerKey === "h") {
          movePaneFocus("left");
          return;
        }
        if (lowerKey === "j") {
          movePaneFocus("down");
          return;
        }
        if (lowerKey === "k") {
          movePaneFocus("up");
          return;
        }
        if (lowerKey === "l") {
          movePaneFocus("right");
          return;
        }

        if (lowerKey === "x") {
          closeFocusedPane();
          return;
        }

        if (lowerKey === "z") {
          setZoomedPaneId((current) => (current === focusedPaneId ? null : focusedPaneId));
          focusPane(focusedPaneId);
          return;
        }

        if (key === ",") {
          setSettingsOpen(true);
        }
        return;
      }

      if (modalOpen) return;

      if (event.ctrlKey && !event.metaKey && lowerKey === "a" && !isEditableTarget(event.target)) {
        event.preventDefault();
        armLeader();
      } else if ((event.metaKey || event.ctrlKey) && lowerKey === "k") {
        event.preventDefault();
        setSwitcherOpen((open) => !open);
      } else if ((event.metaKey || event.ctrlKey) && lowerKey === "b") {
        event.preventDefault();
        setSidebarVisible((visible) => !visible);
      } else if (handlePaneScopeKey(event)) {
        event.preventDefault();
      }
    }

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [
    activeGroup,
    activeGroupIndex,
    activeItemIndex,
    closeFocusedPane,
    disarmLeader,
    fleetGroups,
    focusedPaneId,
    leaderArmed,
    modalOpen,
    movePaneFocus,
    handlePaneScopeKey,
    openAgentInContext,
  ]);

  const isEditorMode = mode === "edit" || mode === "new";

  useEffect(() => {
    if (refreshTick === 0 || isEditorMode) return;
    const activePath = mode === "view" ? activeNote?.path ?? null : null;
    let disposed = false;

    (async () => {
      try {
        const list = await listNotes();
        if (disposed) return;
        setNotes((prev) => (notesFingerprint(prev) === notesFingerprint(list) ? prev : list));
        if (activePath) {
          const fresh = await getNote(activePath);
          if (disposed) return;
          setActiveNote((prev) =>
            prev &&
            prev.path === fresh.path &&
            prev.updated_at === fresh.updated_at &&
            prev.content === fresh.content
              ? prev
              : fresh
          );
        }
      } catch {
        // transient failure; next event retries
      }
    })();

    return () => {
      disposed = true;
    };
  }, [refreshTick]);

  function toggleFolder(path: string) {
    setCollapsedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  function collapseAll() {
    setCollapsedFolders((current) => {
      const all = collectFolderPaths(tree);
      return current.size === all.length ? new Set() : new Set(all);
    });
  }

  const isEditor = mode === "edit" || mode === "new";
  const canSave = mode === "edit" || draft.title.trim().length > 0;
  const utilityTitles: Partial<Record<Mode, string>> = {
    activity: "Activity",
    graph: "Graph",
    health: "Health",
    agents: "Agents"
  };
  const themeToggleTarget = toggleThemePolarity(theme);
  const themeToggleTargetLabel = getTheme(themeToggleTarget).label;
  const currentThemeIsDark = isDarkTheme(theme);
  const tabTitle =
    mode === "new"
      ? "Untitled"
      : mode === "agent"
        ? agentTicket ?? "Agent"
        : utilityTitles[mode] ?? (activeNote ? basename(activeNote.path) : "New tab");
  const breadcrumbs =
    mode === "new"
      ? ["Untitled"]
      : mode === "agent"
        ? ["Agents", agentTicket ?? ""]
        : utilityTitles[mode]
          ? [utilityTitles[mode]!]
          : activeNote
            ? activeNote.path.replace(/\.md$/, "").split("/")
            : [];
  const activeParsed = useMemo(
    () => splitFrontmatter(activeNote?.content ?? ""),
    [activeNote?.content]
  );
  const status = countWords(
    isEditor ? draft.content : prepareMarkdown(activeParsed.body)
  );
  const isKanban =
    activeParsed.properties?.some(([key, value]) => key === "view" && value === "kanban") ??
    false;

  function handlePaneDrop(targetKey: string, zone: DropZone) {
    setZoomedPaneId(null);
    const path = draggingNotePath;
    setDraggingNotePath(null);
    if (!path) return;

    if (zone === "center") {
      if (targetKey === "primary") {
        if (path.startsWith("agent://")) {
          openAgent(path.slice("agent://".length));
        } else {
          openNote(path);
        }
        focusPane("primary");
      } else {
        setLayout((current) => replaceNotePane(current, targetKey, path));
        focusPane(targetKey);
      }
      return;
    }

    paneIdRef.current += 1;
    const newPane: Layout = { kind: "note", id: `pane-${paneIdRef.current}`, path };
    setLayout((current) => splitLayout(current, targetKey, zone, newPane));
    focusPane(newPane.id);
  }

  function renderPaneFrame(key: string, child: ReactNode) {
    return (
      <div
        data-pane-key={key}
        className={`pane-frame${focusedPaneId === key ? " is-focused" : ""}`}
        ref={(node) => registerPaneRef(key, node)}
        tabIndex={-1}
        onFocusCapture={() => setFocusedPaneId(key)}
        onMouseDownCapture={() => setFocusedPaneId(key)}
      >
        {child}
      </div>
    );
  }

  function renderLayout(node: Layout, primaryContent: ReactNode, path: number[]): ReactNode {
    if (zoomedPaneId && node.kind === "split") {
      if (layoutContains(node.first, zoomedPaneId)) {
        return renderLayout(node.first, primaryContent, [...path, 1]);
      }
      if (layoutContains(node.second, zoomedPaneId)) {
        return renderLayout(node.second, primaryContent, [...path, 2]);
      }
    }

    if (node.kind === "primary") {
      return (
        <PaneDropTarget
          active={draggingNotePath !== null}
          key="primary"
          onDropZone={(zone) => handlePaneDrop("primary", zone)}
        >
          {renderPaneFrame("primary", primaryContent)}
        </PaneDropTarget>
      );
    }
    if (node.kind === "note") {
      return (
        <PaneDropTarget
          active={draggingNotePath !== null}
          key={node.id}
          onDropZone={(zone) => handlePaneDrop(node.id, zone)}
        >
          {renderPaneFrame(
            node.id,
            <SecondaryPane
              agentWorkers={agentWorkers}
              notes={notes}
              path={node.path}
              refreshTick={refreshTick}
              onClose={() =>
                setLayout((current) => closeNotePane(current, node.id) ?? { kind: "primary" })
              }
              onOpenNote={openNote}
            />
          )}
        </PaneDropTarget>
      );
    }
    return (
      <div className={`pane-split ${node.direction}`} key={path.join(".")}>
        <div className="pane-cell" style={{ flexGrow: node.ratio }}>
          {renderLayout(node.first, primaryContent, [...path, 1])}
        </div>
        <PaneDivider
          direction={node.direction}
          onRatio={(ratio) => setLayout((current) => setSplitRatio(current, path, ratio))}
        />
        <div className="pane-cell" style={{ flexGrow: 1 - node.ratio }}>
          {renderLayout(node.second, primaryContent, [...path, 2])}
        </div>
      </div>
    );
  }

  return (
    <div className={`app-container${sidebarVisible ? "" : " sidebar-hidden"}`}>
      <div className="workspace-ribbon">
        <button
          aria-label="New note"
          className="ribbon-action"
          title="New note"
          type="button"
          onClick={startNewNote}
        >
          <SquarePen size={18} />
        </button>
        <button
          aria-label="Files"
          className={`ribbon-action${sidebarTab === "files" ? " is-active" : ""}`}
          title="Files"
          type="button"
          onClick={() => setSidebarTab("files")}
        >
          <FolderIcon size={18} />
        </button>
        <button
          aria-label="Search"
          className={`ribbon-action${sidebarTab === "search" ? " is-active" : ""}`}
          title="Search"
          type="button"
          onClick={() => setSidebarTab("search")}
        >
          <Search size={18} />
        </button>
        <button
          aria-label="Agent list"
          className={`ribbon-action${sidebarTab === "agents" ? " is-active" : ""}`}
          title="Agent list"
          type="button"
          onClick={() => setSidebarTab("agents")}
        >
          <SquareTerminal size={18} />
        </button>
        <button
          aria-label="Activity feed"
          className={`ribbon-action${mode === "activity" ? " is-active" : ""}`}
          title="Activity feed"
          type="button"
          onClick={() => openUtilityView("activity")}
        >
          <History size={18} />
        </button>
        <button
          aria-label="Graph view"
          className={`ribbon-action${mode === "graph" ? " is-active" : ""}`}
          title="Graph view"
          type="button"
          onClick={() => openUtilityView("graph")}
        >
          <Waypoints size={18} />
        </button>
        <button
          aria-label="Vault health"
          className={`ribbon-action${mode === "health" ? " is-active" : ""}`}
          title="Vault health"
          type="button"
          onClick={() => openUtilityView("health")}
        >
          <HeartPulse size={18} />
        </button>
        <button
          aria-label="Agents"
          className={`ribbon-action${mode === "agents" ? " is-active" : ""}`}
          title="Agents"
          type="button"
          onClick={() => openUtilityView("agents")}
        >
          <Bot size={18} />
        </button>
        <div className="ribbon-spacer" />
        <button
          aria-label="Settings"
          className="ribbon-action"
          title="Settings"
          type="button"
          onClick={() => setSettingsOpen(true)}
        >
          <Settings size={18} />
        </button>
        <button
          aria-label={currentThemeIsDark ? "Switch to light theme" : "Switch to dark theme"}
          className="ribbon-action"
          title={`Switch to ${themeToggleTargetLabel}`}
          type="button"
          onClick={() => setTheme((current) => toggleThemePolarity(current))}
        >
          <span className="theme-icon-stack">
            <Sun className={`theme-icon${currentThemeIsDark ? " is-active" : ""}`} size={18} />
            <Moon className={`theme-icon${currentThemeIsDark ? "" : " is-active"}`} size={18} />
          </span>
        </button>
      </div>

      <aside className="workspace-sidebar">
        {sidebarTab === "files" ? (
          <>
            <div className="nav-header">
              <div className="nav-buttons-container">
                <button
                  aria-label="New note"
                  className="nav-action-button"
                  title="New note"
                  type="button"
                  onClick={startNewNote}
                >
                  <FilePlus2 size={16} />
                </button>
                <button
                  aria-label="Collapse all"
                  className="nav-action-button"
                  title="Collapse all"
                  type="button"
                  onClick={collapseAll}
                >
                  <ChevronsDownUp size={16} />
                </button>
              </div>
            </div>
            <div className="nav-files-container">
              {isLoading ? (
                <div className="nav-empty">
                  <LoadingPlaceholder className="nav-loading" lines={[92, 86, 88, 74, 81]} />
                </div>
              ) : notes.length > 0 ? (
                <FolderTree
                  activePath={mode === "view" || mode === "edit" ? activeNote?.path ?? null : null}
                  collapsed={collapsedFolders}
                  depth={0}
                  dragActive={draggingNotePath !== null}
                  folder={tree}
                  onContextMenu={handleTreeContextMenu}
                  onDropOnFolder={handleDropOnFolder}
                  onNoteDragEnd={() => setDraggingNotePath(null)}
                  onNoteDragStart={setDraggingNotePath}
                  onOpenNote={openNote}
                  onToggleFolder={toggleFolder}
                />
              ) : (
                <div className="nav-empty">No notes yet</div>
              )}
            </div>
          </>
        ) : sidebarTab === "search" ? (
          <div className="search-panel">
            <div className="search-input-container">
              <input
                placeholder="Search..."
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <div className="search-results">
              {query.trim() === "" ? (
                <div className="nav-empty">Type to start searching</div>
              ) : searchResults.length > 0 ? (
                searchResults.map((note) => (
                  <button
                    className="search-result"
                    key={note.id}
                    type="button"
                    onClick={() => openNote(note.path)}
                  >
                    <span className="search-result-title">{basename(note.path)}</span>
                    <span className="search-result-path">{note.path}</span>
                    {note.excerpt ? (
                      <span className="search-result-excerpt">{note.excerpt}</span>
                    ) : null}
                  </button>
                ))
              ) : (
                <div className="nav-empty">No matches found</div>
              )}
            </div>
          </div>
        ) : (
          <AgentsSidebar
            activeTicket={mode === "agent" ? agentTicket : null}
            data={agentsState}
            refreshTick={refreshTick}
            onDragEnd={() => setDraggingNotePath(null)}
            onDragStart={(ticket) => setDraggingNotePath(`agent://${ticket}`)}
            onOpen={openAgent}
          />
        )}
      </aside>

      <main className="workspace-leaf">
        <div className="workspace-tab-header">
          <div className="workspace-tab">
            <span>{tabTitle}</span>
          </div>
        </div>

        <div className={`view-header${mode === "agent" ? " is-hidden" : ""}`}>
          <div className="view-header-title-container">
            {breadcrumbs.map((crumb, index) => (
              <span className="view-header-breadcrumb" key={`${crumb}-${index}`}>
                {index > 0 ? <ChevronRight size={14} /> : null}
                <span>{crumb}</span>
              </span>
            ))}
          </div>
          <div className="view-actions">
            {mode === "view" ? (
              <button
                aria-label="Edit this note"
                className="view-action"
                title="Edit this note"
                type="button"
                onClick={startEditing}
              >
                <Pencil size={16} />
              </button>
            ) : null}
            {isEditor ? (
              <>
                <button
                  aria-label="Save and read"
                  className="view-action"
                  disabled={!canSave || isSaving}
                  title="Save and switch to reading view"
                  type="button"
                  onClick={saveDraft}
                >
                  <BookOpen size={16} />
                </button>
                <button
                  aria-label="Discard changes"
                  className="view-action"
                  title="Discard changes"
                  type="button"
                  onClick={cancelEditing}
                >
                  <X size={16} />
                </button>
              </>
            ) : null}
          </div>
        </div>

        {error ? (
          <div className="notice" role="alert">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="workspace-panes">
          {renderLayout(
            layout,
            <div className="view-content" ref={viewContentRef}>
          {mode === "activity" ? (
            <ActivityFeed onOpenNote={openNote} refreshTick={refreshTick} />
          ) : mode === "graph" ? (
            <GraphView onOpenNote={openNote} />
          ) : mode === "health" ? (
            <HealthView notes={notes} onOpenNote={openNote} />
          ) : mode === "agent" && agentTicket ? (
            <AgentSessionView
              initialPanel={agentPanel}
              key={agentTicket}
              refreshTick={refreshTick}
              worker={activeAgentWorker ?? { ticket: agentTicket }}
            />
          ) : mode === "agents" ? (
            <AgentsView
              data={agentsState}
              onOpenAgent={openAgent}
              refreshTick={refreshTick}
              openTicket={agentsOpenTicket}
              onOpenTicket={setAgentsOpenTicket}
              accountEvents={accountEvents}
            />
          ) : mode === "empty" ? (
            <div className="empty-state">
              <div className="empty-state-title">No file is open</div>
              <div className="empty-state-actions">
                <button type="button" onClick={startNewNote}>
                  Create new note
                </button>
                {notes.length > 0 ? (
                  <button type="button" onClick={() => openNote(notes[0].path)}>
                    Open most recent note
                  </button>
                ) : null}
              </div>
            </div>
          ) : mode === "view" && activeNote ? (
            <div className="markdown-reading-view" key={activeNote.path}>
              <div className={`markdown-sizer${isKanban ? " kanban-sizer" : ""}`}>
                <h1 className="inline-title">{activeNote.title}</h1>
                {!isKanban && activeParsed.properties && activeParsed.properties.length > 0 ? (
                  <div className="metadata-container" aria-label="Properties">
                    {activeParsed.properties.map(([key, value]) => (
                      <div className="metadata-property" key={key}>
                        <span className="metadata-property-key">{key}</span>
                        <span className="metadata-property-value">
                          {Array.isArray(value) ? (
                            value.map((item) => (
                              <span className="metadata-pill" key={item}>
                                {item}
                              </span>
                            ))
                          ) : (
                            value
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {isKanban ? (
                  <KanbanBoard
                    content={activeNote.content}
                    notes={notes}
                    onChange={saveKanbanContent}
                    onComplete={completeKanbanCard}
                    onOpenNote={openNote}
                  />
                ) : (
                  <div className="markdown-preview-view">
                    <ObsidianMarkdown
                      content={stripLeadingTitle(activeParsed.body, activeNote.title)}
                      notes={notes}
                      onCreateNote={promptCreateUnresolved}
                      onOpenNote={openNote}
                    />
                  </div>
                )}
                {(links[activeNote.path]?.incoming.length ?? 0) > 0 ? (
                  <div className="backlinks">
                    <div className="backlinks-heading">
                      Linked mentions
                      <span className="backlinks-count">
                        {links[activeNote.path].incoming.length}
                      </span>
                    </div>
                    <div className="backlinks-list">
                      {links[activeNote.path].incoming.map((linkPath) => (
                        <button
                          className="backlink"
                          key={linkPath}
                          type="button"
                          onClick={() => openNote(linkPath)}
                        >
                          <span className="backlink-name">{basename(linkPath)}</span>
                          <span className="backlink-path">{linkPath}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ) : null}
              </div>
            </div>
          ) : isEditor ? (
            <div className="markdown-source-view">
              <div className="markdown-sizer">
                {mode === "new" ? (
                  <div className="new-note-meta">
                    <input
                      className="inline-title-input"
                      placeholder="Untitled"
                      type="text"
                      value={draft.title}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, title: event.target.value }))
                      }
                    />
                    <input
                      className="note-path-input"
                      placeholder="folder/note.md (optional)"
                      type="text"
                      value={draft.path}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, path: event.target.value }))
                      }
                    />
                  </div>
                ) : (
                  <h1 className="inline-title">{draft.title}</h1>
                )}
                <textarea
                  className="source-editor"
                  spellCheck="true"
                  value={draft.content}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, content: event.target.value }))
                  }
                />
              </div>
            </div>
          ) : null}
            </div>,
            []
          )}
        </div>

        <div className="status-bar">
          <div className="tmux-status">
            <div className="tmux-group-list">
              {fleetGroups.map((group) => (
                <button
                  className={`tmux-group-button${
                    activeGroup?.orch.id === group.orch.id ? " is-active" : ""
                  }`}
                  key={group.orch.id}
                  type="button"
                  onClick={() => {
                    setActiveGroupId(group.orch.id);
                    openAgentInContext(group.entryTicket);
                  }}
                >
                  {group.orch.id}
                </button>
              ))}
            </div>
            {activeGroup ? (
              <div className="tmux-group-items">
                {activeGroup.items.map((item, index) => (
                  <button
                    className={`tmux-status-item${
                      activeGroupTicket === item.ticket ? " is-active" : ""
                    }`}
                    key={item.ticket}
                    type="button"
                    onClick={() => openAgentInContext(item.ticket)}
                  >
                    <span className="tmux-status-index">{index}</span>
                    <span className="tmux-status-sep">:</span>
                    <span className="tmux-status-label">{item.label}</span>
                    <span className="tmux-status-glyph">
                      {agentStateGlyph(item.state, item.live)}
                    </span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="tmux-group-empty">{agentsState.error ?? "No orchestrators"}</div>
            )}
          </div>
          {mode === "view" || mode === "edit" || mode === "new" ? (
            <div className="status-bar-metrics">
              <span>{status.words} words</span>
              <span>{status.characters} characters</span>
            </div>
          ) : null}
        </div>
      </main>

      {contextMenu ? (
        <div
          className="context-menu"
          style={{ left: contextMenu.x, top: contextMenu.y }}
        >
          {contextMenu.kind === "file" ? (
            <>
              <button
                type="button"
                onClick={() => {
                  const { path } = contextMenu;
                  setContextMenu(null);
                  promptRename(path);
                }}
              >
                Rename / move…
              </button>
              <button
                className="is-danger"
                type="button"
                onClick={() => {
                  const { path } = contextMenu;
                  setContextMenu(null);
                  promptDelete(path);
                }}
              >
                Delete
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => {
                const { path } = contextMenu;
                setContextMenu(null);
                promptNewNoteIn(path);
              }}
            >
              New note…
            </button>
          )}
        </div>
      ) : null}

      {dialog ? <Dialog dialog={dialog} onClose={() => setDialog(null)} /> : null}

      {settingsOpen ? (
        <SettingsModal theme={theme} onClose={() => setSettingsOpen(false)} onThemeChange={setTheme} />
      ) : null}
      {switcherOpen ? (
        <QuickSwitcher
          notes={notes}
          onClose={() => setSwitcherOpen(false)}
          onOpen={(path) => {
            setSwitcherOpen(false);
            openNote(path);
          }}
          onOpenPage={(page) => {
            setSwitcherOpen(false);
            openUtilityView(page);
          }}
        />
      ) : null}
      {fleetChooser ? (
        <FleetSwitcher
          items={fleetChooser === "orchestrators" ? orchestratorChooserItems : fleetTreeItems}
          title={fleetChooser === "orchestrators" ? "Choose orchestrator" : "Choose agent run"}
          onClose={() => setFleetChooser(null)}
          onPick={(item) => {
            setFleetChooser(null);
            openAgentInContext(item.value);
          }}
        />
      ) : null}
      {leaderArmed ? <div className="leader-indicator">C-a</div> : null}
    </div>
  );
}
