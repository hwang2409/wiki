import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";
import {
  AlertCircle,
  BookOpen,
  Bot,
  ClipboardList,
  Code2,
  ChevronRight,
  ChevronsDownUp,
  FilePlus2,
  Files,
  FileCode2,
  FileImage,
  FileJson,
  FileText,
  File as FileIcon,
  Folder as FolderIcon,
  HeartPulse,
  History,
  Lock,
  Moon,
  Pencil,
  Search,
  Settings,
  SquarePen,
  SquareTerminal,
  Terminal as TerminalIcon,
  Sun,
  TrendingUp,
  Waypoints,
  X
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  createNote,
  deleteNote,
  getAgents,
  getFileContent,
  getLinks,
  getNote,
  listFiles,
  listNotes,
  listWorkspaces,
  renameNote,
  searchNotes,
  updateNote
} from "./api";
import {
  ApiError,
  type AgentWorker,
  type ArchivedWorker,
  type FileSummary,
  type NoteLinks,
  type Orchestrator,
  type Workspace,
} from "./api";
import {
  FleetSwitcher,
  QuickSwitcher,
  type FleetSwitcherItem,
  type RecentSwitcherItem,
  type QuickSwitcherSession,
} from "./switcher";
import {
  buildTree,
  filePanePath,
  isActiveFilePath,
  MAX_RECENT_RESOURCES,
  normalizeFilePanePath,
  parseFilePanePath,
  parseStoredRecentResources,
  reconcileWorkspaceState,
  shouldAcceptWorkspaceResponse,
  shouldDiscoverWorkspaces,
  type TreeFile,
  type TreeFolder,
  WorkspaceRequestTracker,
  workspaceCacheKey,
} from "./file-workspaces";
import { SettingsModal, applyStoredFonts } from "./settings";
import { ActivityFeed } from "./activity";
import { AgentsSidebar, AgentsView, type AccountEvent } from "./agents";
import {
  AgentSessionView,
  clearSurfacePaneState,
  type AgentRoutePanel,
  type AgentSessionSurfaceWorker,
} from "./agent-session-surface";
import { LoadingPlaceholder } from "./loading";
import { GraphView } from "./graph";
import { HealthView } from "./health";
import { TokensView } from "./tokens";
import { DashboardView } from "./dashboard";
import { appendDoneEntry } from "./kanban";
import { WorkspacePane, type PaneNoteFocusState } from "./pane";
import { prepareMarkdown, splitFrontmatter } from "./markdown";
import { clearSessionPaneState } from "./session";
import type { TerminalPaneController } from "./terminal-pane";
import { disposeTerminalRuntime } from "./terminal-runtime";
import { invalidateTranscript } from "./transcript-store";
import {
  applyTheme,
  getStoredTheme,
  getTheme,
  isDarkTheme,
  toggleThemePolarity,
  type ThemeId
} from "./themes";
import type { Note, NoteDraft, NoteSummary } from "./types";

type Mode =
  | "empty"
  | "view"
  | "file"
  | "edit"
  | "new"
  | "activity"
  | "graph"
  | "health"
  | "agents"
  | "tokens"
  | "dashboard"
  | "agent"
  | "terminal";
type UtilityMode = "activity" | "graph" | "health" | "agents" | "tokens" | "dashboard";
type SidebarTab = "files" | "search" | "agents";
type SplitPosition = "left" | "right" | "top" | "bottom";
type DropZone = SplitPosition | "center";
type ResourceKind = "note" | "file";

type Layout =
  | { kind: "pane"; id: string; path: string | null; resourceKind?: ResourceKind }
  | { kind: "split"; direction: "row" | "column"; ratio: number; first: Layout; second: Layout };

type WorkspaceWindow = {
  id: string;
  layout: Layout;
  focusedPaneId: string;
};

type WindowWorkspaceState = {
  activeWindowId: string | null;
  windows: WorkspaceWindow[];
};

type StoredWindowWorkspaceState = WindowWorkspaceState & {
  version: 2;
};

type LegacyLayout =
  | { kind: "primary" }
  | { kind: "note"; id: string; path: string }
  | {
      kind: "split";
      direction: "row" | "column";
      ratio: number;
      first: LegacyLayout;
      second: LegacyLayout;
    };

type LegacyStoredLayoutState = {
  focusedPaneId?: string;
  layout: LegacyLayout;
  primaryPath?: string | null;
  version?: 1;
};

type PaneInfo = {
  key: string;
  kind: "agent" | "blank" | "file" | "note" | "terminal" | "utility";
  path: string | null;
  resourceKind: ResourceKind | null;
  ticket: string | null;
};

type FileWorkspaceState = {
  files: FileSummary[];
  truncated: boolean;
  loaded: boolean;
  loading: boolean;
};

type PaneGeometry = {
  key: string;
  order: number;
  x: number;
  y: number;
};

type WindowChooserKind = "agent" | "note";

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

const WINDOWS_STORAGE_KEY = "wiki-window-layout-v2";
const LEGACY_WINDOWS_STORAGE_KEY = "wiki-window-layout-v1";

function isPanePath(path: unknown): path is string {
  return typeof path === "string" && path.length > 0;
}

function isAgentPath(path: string | null): path is string {
  return Boolean(path?.startsWith("agent://"));
}

function isTerminalPath(path: string | null): path is string {
  return Boolean(path?.startsWith("terminal://"));
}

function utilityKindFromPanePath(path: string | null): UtilityMode | null {
  const kind = path?.startsWith("utility://") ? path.slice("utility://".length) : null;
  return kind && (UTILITY_ROUTES as readonly string[]).includes(kind) ? (kind as UtilityMode) : null;
}

function utilityPanePath(kind: UtilityMode): string {
  return `utility://${kind}`;
}

function utilityLabel(kind: UtilityMode): string {
  return `${kind.slice(0, 1).toUpperCase()}${kind.slice(1)}`;
}

function paneKind(path: string | null, resourceKind?: ResourceKind): PaneInfo["kind"] {
  if (path === null) return "blank";
  if (isAgentPath(path)) return "agent";
  if (isTerminalPath(path)) return "terminal";
  if (utilityKindFromPanePath(path)) return "utility";
  return resourceKind === "file" ? "file" : "note";
}

function splitLayout(
  node: Layout,
  targetKey: string,
  position: SplitPosition,
  newPane: Layout
): Layout {
  if (node.kind === "pane" && node.id === targetKey) {
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

function replacePane(node: Layout, id: string, replacement: Extract<Layout, { kind: "pane" }>): Layout {
  if (node.kind === "pane" && node.id === id) return replacement;
  if (node.kind === "split") {
    return {
      ...node,
      first: replacePane(node.first, id, replacement),
      second: replacePane(node.second, id, replacement)
    };
  }
  return node;
}

function replacePanePath(
  node: Layout,
  id: string,
  path: string | null,
  resourceKind?: ResourceKind
): Layout {
  if (node.kind === "pane" && node.id === id) return { ...node, path, resourceKind };
  if (node.kind === "split") {
    return {
      ...node,
      first: replacePanePath(node.first, id, path, resourceKind),
      second: replacePanePath(node.second, id, path, resourceKind)
    };
  }
  return node;
}

function replaceMatchingPanePaths(node: Layout, currentPath: string, nextPath: string): Layout {
  if (node.kind === "pane") {
    return node.path === currentPath ? { ...node, path: nextPath } : node;
  }
  return {
    ...node,
    first: replaceMatchingPanePaths(node.first, currentPath, nextPath),
    second: replaceMatchingPanePaths(node.second, currentPath, nextPath),
  };
}

function setSplitRatio(node: Layout, path: number[], ratio: number): Layout {
  if (node.kind !== "split") return node;
  if (path.length === 0) return { ...node, ratio };
  const [head, ...rest] = path;
  return head === 1
    ? { ...node, first: setSplitRatio(node.first, rest, ratio) }
    : { ...node, second: setSplitRatio(node.second, rest, ratio) };
}

function removePane(node: Layout, id: string): { layout: Layout | null; removedPath: string | null } {
  if (node.kind === "pane" && node.id === id) {
    return { layout: null, removedPath: node.path };
  }
  if (node.kind === "split") {
    const first = removePane(node.first, id);
    const second = removePane(node.second, id);
    if (first.layout === null) return { layout: second.layout, removedPath: first.removedPath };
    if (second.layout === null) return { layout: first.layout, removedPath: second.removedPath };
    return {
      layout: { ...node, first: first.layout, second: second.layout },
      removedPath: first.removedPath ?? second.removedPath
    };
  }
  return { layout: node, removedPath: null };
}

function removePanePaths(node: Layout, predicate: (path: string) => boolean): Layout | null {
  if (node.kind === "pane") return node.path !== null && predicate(node.path) ? null : node;
  const first = removePanePaths(node.first, predicate);
  const second = removePanePaths(node.second, predicate);
  if (first === null) return second;
  if (second === null) return first;
  return { ...node, first, second };
}

function layoutContains(node: Layout, key: string): boolean {
  if (node.kind === "pane") return node.id === key;
  return layoutContains(node.first, key) || layoutContains(node.second, key);
}

function paneInfo(node: Extract<Layout, { kind: "pane" }>): PaneInfo {
  const kind = paneKind(node.path, node.resourceKind);
  return {
    key: node.id,
    kind,
    path: node.path,
    resourceKind: kind === "note" || kind === "file" ? node.resourceKind ?? "note" : null,
    ticket: ticketFromPanePath(node.path),
  };
}

function collectPaneInfos(node: Layout, panes: PaneInfo[] = []): PaneInfo[] {
  if (node.kind === "pane") {
    panes.push(paneInfo(node));
    return panes;
  }
  collectPaneInfos(node.first, panes);
  collectPaneInfos(node.second, panes);
  return panes;
}

function collectPaneGeometry(
  node: Layout,
  bounds: { height: number; width: number; x: number; y: number } = {
    height: 1,
    width: 1,
    x: 0,
    y: 0,
  },
  panes: PaneGeometry[] = []
): PaneGeometry[] {
  if (node.kind === "pane") {
    panes.push({
      key: node.id,
      order: panes.length,
      x: bounds.x,
      y: bounds.y,
    });
    return panes;
  }

  if (node.direction === "row") {
    const firstWidth = bounds.width * node.ratio;
    collectPaneGeometry(
      node.first,
      {
        ...bounds,
        width: firstWidth,
      },
      panes
    );
    collectPaneGeometry(
      node.second,
      {
        height: bounds.height,
        width: bounds.width - firstWidth,
        x: bounds.x + firstWidth,
        y: bounds.y,
      },
      panes
    );
    return panes;
  }

  const firstHeight = bounds.height * node.ratio;
  collectPaneGeometry(
    node.first,
    {
      ...bounds,
      height: firstHeight,
    },
    panes
  );
  collectPaneGeometry(
    node.second,
    {
      height: bounds.height - firstHeight,
      width: bounds.width,
      x: bounds.x,
      y: bounds.y + firstHeight,
    },
    panes
  );
  return panes;
}

function orderPaneKeysByVisualPosition(node: Layout): string[] {
  const epsilon = 0.000001;
  return collectPaneGeometry(node)
    .sort((left, right) => {
      if (Math.abs(left.y - right.y) > epsilon) return left.y - right.y;
      if (Math.abs(left.x - right.x) > epsilon) return left.x - right.x;
      return left.order - right.order;
    })
    .map((pane) => pane.key);
}

function firstPaneKey(node: Layout): string {
  if (node.kind === "pane") return node.id;
  return firstPaneKey(node.first);
}

function findPaneInfo(node: Layout, key: string): PaneInfo | null {
  if (node.kind === "pane") {
    return node.id === key ? paneInfo(node) : null;
  }
  return findPaneInfo(node.first, key) ?? findPaneInfo(node.second, key);
}

function ticketFromPanePath(path: string | null): string | null {
  return path?.startsWith("agent://") ? path.slice("agent://".length) : null;
}

function terminalIdFromPanePath(path: string | null): string | null {
  return path?.startsWith("terminal://") ? path.slice("terminal://".length) : null;
}

function cwdBasename(path: string | null): string {
  return path ? path.split("/").slice(-1)[0] : "no cwd";
}

function paneLabel(path: string | null): string {
  if (path === null) return "new pane";
  const ticket = ticketFromPanePath(path);
  if (ticket) return ticket;
  const terminalId = terminalIdFromPanePath(path);
  if (terminalId) return `term:${terminalId.slice(0, 8)}`;
  const utility = utilityKindFromPanePath(path);
  if (utility) return utilityLabel(utility);
  return basename(path);
}

function windowLabel(window: WorkspaceWindow): string {
  const panes = collectPaneInfos(window.layout);
  if (panes.length === 0) return "empty";
  const [first, ...rest] = panes;
  return rest.length > 0 ? `${paneLabel(first.path)}+${rest.length}` : paneLabel(first.path);
}

function normalizeWindow(
  window: WorkspaceWindow,
  seenTickets: Set<string>,
  seenTerminalIds: Set<string>,
  seenUtilities: Set<UtilityMode>
): WorkspaceWindow | null {
  function prune(node: Layout): Layout | null {
    if (node.kind === "pane") {
      if (node.path === null) return node;
      if (!isPanePath(node.path)) return null;
      const resourceKind =
        node.resourceKind ??
        (isAgentPath(node.path) || isTerminalPath(node.path) || utilityKindFromPanePath(node.path)
          ? undefined
          : isMarkdownPath(node.path)
            ? "note"
            : "file");
      const normalizedPath = resourceKind === "file" ? normalizeFilePanePath(node.path) : node.path;
      const ticket = ticketFromPanePath(normalizedPath);
      if (ticket) {
        if (seenTickets.has(ticket)) return null;
        seenTickets.add(ticket);
      }
      const terminalId = terminalIdFromPanePath(normalizedPath);
      if (terminalId) {
        if (seenTerminalIds.has(terminalId)) return null;
        seenTerminalIds.add(terminalId);
      }
      const utility = utilityKindFromPanePath(normalizedPath);
      if (utility) {
        if (seenUtilities.has(utility)) return null;
        seenUtilities.add(utility);
      }
      return { ...node, path: normalizedPath, resourceKind };
    }
    const first = prune(node.first);
    const second = prune(node.second);
    if (first === null) return second;
    if (second === null) return first;
    return { ...node, first, second };
  }

  const layout = prune(window.layout);
  if (!layout) return null;
  const panes = collectPaneInfos(layout);
  if (panes.length === 0) return null;
  return {
    id: window.id,
    layout,
    focusedPaneId: panes.some((pane) => pane.key === window.focusedPaneId)
      ? window.focusedPaneId
      : panes[0].key,
  };
}

function normalizeWindowWorkspaceState(state: WindowWorkspaceState): WindowWorkspaceState {
  const seenWindowIds = new Set<string>();
  const seenTickets = new Set<string>();
  const seenTerminalIds = new Set<string>();
  const seenUtilities = new Set<UtilityMode>();
  const windows = state.windows
    .filter((window): window is WorkspaceWindow => typeof window.id === "string" && window.id.length > 0)
    .filter((window) => {
      if (seenWindowIds.has(window.id)) return false;
      seenWindowIds.add(window.id);
      return true;
    })
    .map((window) => normalizeWindow(window, seenTickets, seenTerminalIds, seenUtilities))
    .filter((window): window is WorkspaceWindow => window !== null);
  const activeWindowId =
    state.activeWindowId && windows.some((window) => window.id === state.activeWindowId)
      ? state.activeWindowId
      : windows[0]?.id ?? null;
  return { activeWindowId, windows };
}

function createSoloWindow(
  windowId: string,
  paneId: string,
  path: string | null,
  resourceKind?: ResourceKind
): WorkspaceWindow {
  return {
    id: windowId,
    layout: { kind: "pane", id: paneId, path, resourceKind },
    focusedPaneId: paneId,
  };
}

function convertLegacyLayout(node: LegacyLayout, primaryPath: string | null): Layout | null {
  if (node.kind === "primary") {
    return primaryPath
      ? { kind: "pane", id: "primary", path: primaryPath, resourceKind: "note" }
      : null;
  }
  if (node.kind === "note") {
    return isPanePath(node.path)
      ? { kind: "pane", id: node.id, path: node.path, resourceKind: "note" }
      : null;
  }
  const first = convertLegacyLayout(node.first, primaryPath);
  const second = convertLegacyLayout(node.second, primaryPath);
  if (first === null) return second;
  if (second === null) return first;
  return { ...node, first, second };
}

function readStoredWindowWorkspaceState(): WindowWorkspaceState {
  try {
    const raw = localStorage.getItem(WINDOWS_STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (
        parsed &&
        typeof parsed === "object" &&
        (parsed as { version?: number }).version === 2 &&
        Array.isArray((parsed as StoredWindowWorkspaceState).windows)
      ) {
        return normalizeWindowWorkspaceState(parsed as StoredWindowWorkspaceState);
      }
    }
  } catch {
    // Corrupt state falls through to legacy or default boot.
  }

  try {
    const raw = localStorage.getItem(LEGACY_WINDOWS_STORAGE_KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && "layout" in parsed) {
        const legacy = parsed as LegacyStoredLayoutState;
        const primaryPath = isPanePath(legacy.primaryPath) ? legacy.primaryPath : null;
        const layout = convertLegacyLayout(legacy.layout, primaryPath);
        if (layout) {
          localStorage.removeItem(LEGACY_WINDOWS_STORAGE_KEY);
          return normalizeWindowWorkspaceState({
            activeWindowId: "window-0",
            windows: [
              {
                id: "window-0",
                layout,
                focusedPaneId:
                  typeof legacy.focusedPaneId === "string" ? legacy.focusedPaneId : firstPaneKey(layout),
              },
            ],
          });
        }
      }
    }
  } catch {
    // Legacy migration failures fall back to boot derivation.
  }

  return { activeWindowId: null, windows: [] };
}

function buildAgentSessionWorker(
  worker: Pick<AgentWorker, "ticket" | "kind" | "role" | "model" | "effort" | "pr"> & {
    run_id?: string | null;
  }
): AgentSessionSurfaceWorker {
  return {
    ticket: worker.ticket,
    kind: worker.kind,
    role: worker.role,
    model: worker.model,
    effort: worker.effort,
    pr: worker.pr,
    canReview: Boolean(worker.pr),
    canReplace: Boolean(worker.run_id),
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
        kind: null,
        model: null,
        effort: null,
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

const emptyDraft: NoteDraft = { title: "", path: "", content: "" };

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function isMarkdownPath(path: string) {
  return path.toLowerCase().endsWith(".md");
}

const FILE_ICON_BY_EXTENSION: Record<string, LucideIcon> = {
  ts: FileCode2,
  tsx: FileCode2,
  js: FileCode2,
  jsx: FileCode2,
  mjs: FileCode2,
  cjs: FileCode2,
  py: Code2,
  rb: Code2,
  go: Code2,
  rs: Code2,
  java: Code2,
  c: Code2,
  cc: Code2,
  cpp: Code2,
  h: Code2,
  hpp: Code2,
  json: FileJson,
  yaml: FileJson,
  yml: FileJson,
  toml: FileJson,
  css: Settings,
  scss: Settings,
  sass: Settings,
  less: Settings,
  html: Code2,
  htm: Code2,
  xml: Code2,
  svg: Code2,
  md: FileText,
  markdown: FileText,
  txt: FileText,
  rst: FileText,
  png: FileImage,
  jpg: FileImage,
  jpeg: FileImage,
  gif: FileImage,
  webp: FileImage,
  ico: FileImage,
  bmp: FileImage,
  avif: FileImage,
  env: Lock,
  sh: TerminalIcon,
  bash: TerminalIcon,
  zsh: TerminalIcon,
  fish: TerminalIcon,
  ps1: TerminalIcon,
  bat: TerminalIcon,
  cmd: TerminalIcon,
  conf: Settings,
  config: Settings,
  ini: Settings,
};

function fileIconForPath(path: string): LucideIcon {
  const name = path.slice(path.lastIndexOf("/") + 1).toLowerCase();
  const extensionStart = name.lastIndexOf(".");
  const extension = extensionStart >= 0 ? name.slice(extensionStart + 1) : "";

  if (name.endsWith(".lock") || name.includes(".lock.")) return Lock;
  if (name.endsWith(".config") || name.includes(".config.")) return Settings;
  if (name === ".env" || name === ".gitignore" || name === ".dockerignore") return Lock;
  return FILE_ICON_BY_EXTENSION[extension] ?? FileIcon;
}

function TreeFileIcon({ path }: { path: string }) {
  const Icon = fileIconForPath(path);
  return <Icon className="tree-file-icon" size={13} />;
}

type Route =
  | { kind: "empty" }
  | { kind: "new" }
  | { kind: UtilityMode }
  | { kind: "agent"; ticket: string; panel: AgentRoutePanel }
  | { kind: "terminal"; id: string }
  | { kind: "note" | "edit" | "file"; path: string };

const UTILITY_ROUTES: readonly UtilityMode[] = [
  "activity",
  "graph",
  "health",
  "agents",
  "tokens",
  "dashboard"
];

function routeHash(route: Route): string {
  if (route.kind === "empty") return "#/";
  if (route.kind === "new") return "#/new";
  if (route.kind === "agent") {
    const base = `#/agent/${encodeURIComponent(route.ticket)}`;
    return route.panel === "review" ? `${base}/review` : base;
  }
  if (route.kind === "terminal") return `#/terminal/${encodeURIComponent(route.id)}`;
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
  const terminal = hash.match(/^#\/terminal\/([A-Za-z0-9._-]+)$/);
  if (terminal) return { kind: "terminal", id: terminal[1] };
  const utility = UTILITY_ROUTES.find((kind) => hash === `#/${kind}`);
  if (utility) return { kind: utility };
  const match = hash.match(/^#\/(note|edit|file)\/(.+)$/);
  if (match) {
    const path = match[2].split("/").map(decodeURIComponent).join("/");
    return { kind: match[1] as "note" | "edit" | "file", path };
  }
  return { kind: "empty" };
}

function notesFingerprint(notes: NoteSummary[]) {
  return notes.map((note) => `${note.id}@${note.updated_at}`).join("|");
}

function isTextEntryElement(element: Element | null): boolean {
  if (!element) return false;
  if (
    element instanceof HTMLInputElement ||
    element instanceof HTMLTextAreaElement ||
    element instanceof HTMLSelectElement
  ) {
    return true;
  }
  return element instanceof HTMLElement && element.isContentEditable;
}

function isTextEntryEvent(event: globalThis.KeyboardEvent): boolean {
  return (
    event.isComposing ||
    event.key === "Process" ||
    event.keyCode === 229 ||
    isTextEntryElement(event.target instanceof Element ? event.target : null) ||
    isTextEntryElement(document.activeElement instanceof Element ? document.activeElement : null)
  );
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

const RECENT_RESOURCES_STORAGE_KEY = "wiki-recent-resources";

function recentResourceKey(item: RecentSwitcherItem) {
  return `${item.kind}:${item.workspace}:${item.path}`;
}

function readStoredRecentResources(): RecentSwitcherItem[] {
  return parseStoredRecentResources(localStorage.getItem(RECENT_RESOURCES_STORAGE_KEY));
}

function noteFolderFromTreePath(folderPath: string, showAllFiles: boolean): string | null {
  if (!showAllFiles) return folderPath;
  if (folderPath === "vault") return "";
  if (folderPath.startsWith("vault/")) return folderPath.slice("vault/".length);
  return null;
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
        const parent = divider.parentElement as HTMLElement | null;
        if (!parent) return;
        const rect = parent.getBoundingClientRect();
        divider.setPointerCapture(event.pointerId);
        divider.classList.add("is-dragging");
        document.body.classList.add("is-resizing-panes");

        let latest: number | null = null;
        let frame = 0;
        const apply = () => {
          frame = 0;
          if (latest !== null) parent.style.setProperty("--split-ratio", String(latest));
        };

        const move = (moveEvent: PointerEvent) => {
          const raw =
            direction === "row"
              ? (moveEvent.clientX - rect.left) / rect.width
              : (moveEvent.clientY - rect.top) / rect.height;
          latest = Math.min(0.85, Math.max(0.15, raw));
          if (!frame) frame = requestAnimationFrame(apply);
        };
        const up = () => {
          if (frame) {
            cancelAnimationFrame(frame);
            frame = 0;
          }
          divider.classList.remove("is-dragging");
          document.body.classList.remove("is-resizing-panes");
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
          if (latest !== null) onRatio(latest);
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
  canDropOnFolder,
  onOpenFile,
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
  canDropOnFolder: (path: string) => boolean;
  onOpenFile: (file: TreeFile) => void;
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
                if (!dragActive || !canDropOnFolder(child.path)) return;
                event.preventDefault();
                event.dataTransfer.dropEffect = "move";
                event.currentTarget.classList.add("is-drop-target");
              }}
              onDrop={(event) => {
                if (!dragActive || !canDropOnFolder(child.path)) return;
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
                canDropOnFolder={canDropOnFolder}
                onDropOnFolder={onDropOnFolder}
                onNoteDragEnd={onNoteDragEnd}
                onNoteDragStart={onNoteDragStart}
                onOpenFile={onOpenFile}
                onToggleFolder={onToggleFolder}
              />
            )}
          </div>
        );
      })}
      {folder.files.map((file) => (
        <button
          className={`tree-item-self ${file.isNote ? "nav-file-title" : "nav-code-file-title"}${
            (file.isNote
              ? activePath === file.path
              : isActiveFilePath(activePath, file.workspace ?? "wiki", file.path))
              ? " is-active"
              : ""
          }`}
          draggable={file.isNote}
          key={file.id}
          style={{ paddingInlineStart: `${depth * 17 + 24}px` }}
          type="button"
          onClick={() => onOpenFile(file)}
          onContextMenu={file.isNote ? (event) => onContextMenu(event, "file", file.openPath) : undefined}
          onDragEnd={file.isNote ? onNoteDragEnd : undefined}
          onDragStart={
            file.isNote
              ? (event) => {
                  event.dataTransfer.effectAllowed = "copyMove";
                  event.dataTransfer.setData("text/plain", file.openPath);
                  onNoteDragStart(file.openPath);
                }
              : undefined
          }
        >
          {!file.isNote ? <TreeFileIcon path={file.path} /> : null}
          <span className="tree-item-name">{basename(file.path)}</span>
        </button>
      ))}
    </>
  );
}

export default function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [notesLoaded, setNotesLoaded] = useState(false);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspace, setActiveWorkspace] = useState(
    () => localStorage.getItem("wiki-files-workspace") || "wiki"
  );
  const [filesByWorkspace, setFilesByWorkspace] = useState<Record<string, FileWorkspaceState>>({});
  const [showAllFiles, setShowAllFiles] = useState(
    () => localStorage.getItem("wiki-show-all-files") === "true"
  );
  const [activeNote, setActiveNote] = useState<Note | null>(null);
  const [mode, setMode] = useState<Mode>("empty");
  const [draft, setDraft] = useState<NoteDraft>(emptyDraft);
  const [query, setQuery] = useState("");
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>(() => {
    const stored = localStorage.getItem("wiki-sidebar-tab");
    return stored === "search" || stored === "agents" ? stored : "files";
  });
  const [agentTicket, setAgentTicket] = useState<string | null>(null);
  const [terminalRouteId, setTerminalRouteId] = useState<string | null>(null);
  const [agentPanel, setAgentPanel] = useState<AgentRoutePanel>(null);
  const [leaderArmed, setLeaderArmed] = useState(false);
  const [windowChooserOpen, setWindowChooserOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);

  useEffect(() => {
    applyStoredFonts();
  }, []);

  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(readStoredCollapsed);
  const [recentResources, setRecentResources] = useState<RecentSwitcherItem[]>(
    readStoredRecentResources
  );
  const [switcherOpen, setSwitcherOpen] = useState(false);
  const [sidebarVisible, setSidebarVisible] = useState(
    () => localStorage.getItem("wiki-sidebar-visible") !== "false"
  );
  const [draggingNotePath, setDraggingNotePath] = useState<string | null>(null);
  const [windowState, setWindowState] = useState<WindowWorkspaceState>(readStoredWindowWorkspaceState);
  const [zoomedPaneId, setZoomedPaneId] = useState<string | null>(null);
  const [terminalLaunchNonceById, setTerminalLaunchNonceById] = useState<Record<string, number>>({});
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
  const windowIdRef = useRef(0);
  const paneRefs = useRef(new Map<string, HTMLDivElement>());
  const leaderTimerRef = useRef<number | null>(null);
  const leaderArmedRef = useRef(false);
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<ThemeId>(() => getStoredTheme());
  const [agentsOpenTicket, setAgentsOpenTicket] = useState<string | null>(null);
  const viewContentRef = useRef<HTMLDivElement | null>(null);
  const terminalControllersRef = useRef(new Map<string, TerminalPaneController>());
  const openTerminalIdsRef = useRef<Set<string>>(new Set());
  const preserveViewScrollRef = useRef(false);
  const appliedHashRef = useRef<string | null>(null);
  const closedTicketsRef = useRef<Set<string>>(new Set());
  const filesRequestTrackerRef = useRef(new WorkspaceRequestTracker());
  const workspaceRefreshVersionRef = useRef(0);

  function nextPaneId() {
    paneIdRef.current += 1;
    return `pane-${paneIdRef.current}`;
  }

  function nextWindowId() {
    windowIdRef.current += 1;
    return `window-${windowIdRef.current}`;
  }

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
      try {
        const payload = JSON.parse(raw.data) as { type?: string; ticket?: string; surface?: string | null };
        if (!payload || typeof payload.type !== "string") return;
        if (payload.type === "session" && typeof payload.ticket === "string") {
          invalidateTranscript(payload.ticket, payload.surface ?? null);
          return;
        }
        if (
          payload.type === "vault" ||
          payload.type === "agents" ||
          payload.type === "codex_rotation" ||
          payload.type === "codex_limit_no_eligible" ||
          payload.type === "codex_rotation_failed" ||
          payload.type === "codex_auth_dead_revival" ||
          payload.type === "codex_auth_dead_exhausted" ||
          payload.type === "claude_limit_hit"
        ) {
          setRefreshTick((tick) => tick + 1);
        }
        if (
          payload.type === "codex_rotation" ||
          payload.type === "codex_limit_no_eligible" ||
          payload.type === "codex_rotation_failed" ||
          payload.type === "codex_auth_dead_revival" ||
          payload.type === "codex_auth_dead_exhausted" ||
          payload.type === "claude_limit_hit"
        ) {
          setAccountEvents((prior) => [payload as AccountEvent, ...prior].slice(0, 4));
        }
      } catch {
        /* ignore malformed frames */
      }
    };
    return () => source.close();
  }, []);

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
    localStorage.setItem("wiki-collapsed-folders", JSON.stringify([...collapsedFolders]));
  }, [collapsedFolders]);

  useEffect(() => {
    localStorage.setItem("wiki-show-all-files", String(showAllFiles));
  }, [showAllFiles]);

  useEffect(() => {
    localStorage.setItem("wiki-files-workspace", activeWorkspace);
  }, [activeWorkspace]);

  useEffect(() => {
    if (!shouldDiscoverWorkspaces(sidebarTab, switcherOpen)) return;
    let ignore = false;
    listWorkspaces()
      .then((result) => {
        if (ignore) return;
        workspaceRefreshVersionRef.current += 1;
        filesRequestTrackerRef.current.invalidate();
        setWorkspaces(result.workspaces);
        setActiveWorkspace((current) =>
          reconcileWorkspaceState(result.workspaces, current, {}).activeWorkspace
        );
        setFilesByWorkspace((current) =>
          reconcileWorkspaceState(result.workspaces, "wiki", current).cache
        );
      })
      .catch(() => {
        if (!ignore) {
          workspaceRefreshVersionRef.current += 1;
          filesRequestTrackerRef.current.invalidate();
          setWorkspaces([]);
          setActiveWorkspace("wiki");
        }
      });
    return () => {
      ignore = true;
    };
  }, [refreshTick, sidebarTab, switcherOpen]);

  useEffect(() => {
    localStorage.setItem(
      RECENT_RESOURCES_STORAGE_KEY,
      JSON.stringify({ v: 2, entries: recentResources })
    );
  }, [recentResources]);

  useEffect(() => {
    localStorage.setItem(
      WINDOWS_STORAGE_KEY,
      JSON.stringify({ version: 2, ...windowState } satisfies StoredWindowWorkspaceState)
    );
  }, [windowState]);

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
    if (preserveViewScrollRef.current) {
      preserveViewScrollRef.current = false;
      return;
    }
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
        setNotesLoaded(true);
      } catch (err) {
        if (!ignore) {
          setNotesLoaded(false);
          setError(err instanceof Error ? err.message : "Could not load notes");
        }
      } finally {
        if (!ignore) setIsLoading(false);
      }
    }

    boot();
    return () => {
      ignore = true;
    };
  }, []);

  const activeWorkspaceInfo = workspaces.find((workspace) => workspace.id === activeWorkspace);
  const activeFileCacheKey = activeWorkspaceInfo ? workspaceCacheKey(activeWorkspaceInfo) : null;
  const activeFileState = activeFileCacheKey ? filesByWorkspace[activeFileCacheKey] : undefined;
  const files = activeFileState?.files ?? [];
  const filesLoaded = activeFileState?.loaded ?? false;
  const filesLoading = activeFileState?.loading ?? false;
  const filesTruncated = activeFileState?.truncated ?? false;

  useEffect(() => {
    if (
      (!showAllFiles && !switcherOpen) ||
      !activeWorkspaceInfo ||
      !activeWorkspaceInfo.live ||
      activeFileState?.loaded
    ) return;
    const cacheKey = workspaceCacheKey(activeWorkspaceInfo);
    const requestToken = filesRequestTrackerRef.current.begin(cacheKey);
    if (!requestToken) return;
    setFilesByWorkspace((current) => ({
      ...current,
      [cacheKey]: { ...(current[cacheKey] ?? { files: [], truncated: false, loaded: false }), loading: true },
    }));
    const workspace = activeWorkspace;
    const requestedWorkspace = activeWorkspaceInfo;
    const requestVersion = workspaceRefreshVersionRef.current;
    listFiles(workspace)
      .then((nextTree) => {
        if (
          !filesRequestTrackerRef.current.isCurrent(cacheKey, requestToken) ||
          !shouldAcceptWorkspaceResponse(requestVersion, workspaceRefreshVersionRef.current, requestedWorkspace, cacheKey)
        ) {
          return;
        }
        setFilesByWorkspace((current) => ({
          ...current,
          [cacheKey]: { files: nextTree.files, truncated: nextTree.truncated, loaded: true, loading: false },
        }));
      })
      .catch(() => {
        if (
          !filesRequestTrackerRef.current.isCurrent(cacheKey, requestToken) ||
          !shouldAcceptWorkspaceResponse(requestVersion, workspaceRefreshVersionRef.current, requestedWorkspace, cacheKey)
        ) {
          return;
        }
        setFilesByWorkspace((current) => ({
          ...current,
          [cacheKey]: { files: [], truncated: false, loaded: false, loading: false },
        }));
      })
      .finally(() => {
        filesRequestTrackerRef.current.finish(cacheKey, requestToken);
      });
  }, [activeFileState?.loaded, activeWorkspace, activeWorkspaceInfo, refreshTick, showAllFiles, switcherOpen, workspaces]);

  useEffect(() => {
    for (const window of windowState.windows) {
      const windowMatch = window.id.match(/^window-(\d+)$/);
      if (windowMatch) windowIdRef.current = Math.max(windowIdRef.current, Number(windowMatch[1]));
      for (const pane of collectPaneInfos(window.layout)) {
        const paneMatch = pane.key.match(/^pane-(\d+)$/);
        if (paneMatch) paneIdRef.current = Math.max(paneIdRef.current, Number(paneMatch[1]));
      }
    }
  }, [windowState]);

  useEffect(() => {
    const nextOpenTerminalIds = new Set<string>();
    for (const window of windowState.windows) {
      for (const pane of collectPaneInfos(window.layout)) {
        const terminalId = terminalIdFromPanePath(pane.path);
        if (terminalId) nextOpenTerminalIds.add(terminalId);
      }
    }
    for (const terminalId of openTerminalIdsRef.current) {
      if (!nextOpenTerminalIds.has(terminalId)) {
        terminalControllersRef.current.delete(terminalId);
        disposeTerminalRuntime(terminalId);
      }
    }
    openTerminalIdsRef.current = nextOpenTerminalIds;
  }, [windowState]);

  useEffect(() => {
    const liveWorkers = agentsState.workers?.filter((worker) => worker.window_alive);
    if (!liveWorkers) return;
    setWindowState((current) => {
      const openTickets = new Set(
        current.windows.flatMap((window) =>
          collectPaneInfos(window.layout)
            .map((pane) => pane.ticket)
            .filter((ticket): ticket is string => ticket !== null)
        )
      );
      const closedTickets = closedTicketsRef.current;
      const missing = liveWorkers.filter(
        (worker) => !openTickets.has(worker.ticket) && !closedTickets.has(worker.ticket)
      );
      if (missing.length === 0) return current;
      const next = { ...current, windows: [...current.windows] };
      for (const worker of missing) {
        next.windows.push(createSoloWindow(nextWindowId(), nextPaneId(), `agent://${worker.ticket}`));
      }
      if (!next.activeWindowId) next.activeWindowId = next.windows[0]?.id ?? null;
      return normalizeWindowWorkspaceState(next);
    });
  }, [agentsState.workers]);

  const tree = useMemo(
    () => buildTree(notes, showAllFiles ? files : undefined, activeWorkspace),
    [activeWorkspace, files, notes, showAllFiles]
  );
  const liveWorkspaces = workspaces.filter((workspace) => workspace.live);
  const visibleRecentResources = useMemo(() => {
    const notePaths = notesLoaded ? new Set(notes.map((note) => note.path)) : null;
    const filePaths = filesLoaded && !filesTruncated ? new Set(files.map((file) => file.path)) : null;
    return recentResources.filter((item) => {
      if (item.kind === "note") return notePaths === null || notePaths.has(item.path);
      return item.workspace !== activeWorkspace || filePaths === null || filePaths.has(item.path);
    });
  }, [activeWorkspace, files, filesLoaded, filesTruncated, notes, notesLoaded, recentResources]);
  useEffect(() => {
    const visibleKeys = new Set(visibleRecentResources.map(recentResourceKey));
    setRecentResources((current) => {
      const next = current.filter((item) => visibleKeys.has(recentResourceKey(item)));
      return next.length === current.length ? current : next;
    });
  }, [visibleRecentResources]);
  const activeWindow = useMemo(
    () =>
      windowState.windows.find((window) => window.id === windowState.activeWindowId) ??
      windowState.windows[0] ??
      null,
    [windowState]
  );
  const activeWindowIndex = activeWindow
    ? windowState.windows.findIndex((window) => window.id === activeWindow.id)
    : -1;
  const paneInfos = useMemo(
    () => (activeWindow ? collectPaneInfos(activeWindow.layout) : []),
    [activeWindow]
  );
  const paneMap = useMemo(() => new Map(paneInfos.map((pane) => [pane.key, pane])), [paneInfos]);
  const orderedPaneInfos = useMemo(() => {
    if (!activeWindow) return [];
    const panesByKey = new Map(paneInfos.map((pane) => [pane.key, pane]));
    return orderPaneKeysByVisualPosition(activeWindow.layout)
      .map((key) => panesByKey.get(key))
      .filter((pane): pane is PaneInfo => pane !== undefined);
  }, [activeWindow, paneInfos]);
  const focusedPaneId = activeWindow?.focusedPaneId ?? null;
  const focusedPane = focusedPaneId ? paneMap.get(focusedPaneId) ?? null : null;
  const focusedPanePath = focusedPane?.path ?? null;
  const focusedPaneTicket = focusedPane?.ticket ?? null;
  const focusedTerminalId = terminalIdFromPanePath(focusedPanePath);
  const agentWorkers = useMemo(() => {
    const map = new Map<string, AgentSessionSurfaceWorker>();
    for (const worker of agentsState.workers ?? []) {
      map.set(worker.ticket, buildAgentSessionWorker(worker));
    }
    for (const worker of agentsState.archived ?? []) {
      if (!map.has(worker.ticket)) {
        map.set(worker.ticket, { ...buildAgentSessionWorker(worker), canReplace: false });
      }
    }
    for (const orch of agentsState.orchestrators) {
      map.set(orch.id, {
        ticket: orch.id,
        kind: orch.kind,
        role: "orchestrator",
        model: orch.model,
        effort: orch.effort,
        canReplace: Boolean(orch.run_id),
      });
    }
    return map;
  }, [agentsState.archived, agentsState.orchestrators, agentsState.workers]);
  const quickSwitcherSessions = useMemo<QuickSwitcherSession[]>(() => {
    const sessions: QuickSwitcherSession[] = [];
    for (const orch of agentsState.orchestrators) {
      if (!orch.window_alive) continue;
      sessions.push({
        id: orch.id,
        model: orch.model,
        orchestratorId: null,
        provider: orch.kind,
        role: "orchestrator",
        sessionKind: "orchestrator",
      });
    }
    for (const worker of agentsState.workers ?? []) {
      if (!worker.window_alive) continue;
      sessions.push({
        id: worker.ticket,
        model: worker.model,
        orchestratorId: worker.orch,
        provider: worker.kind,
        role: worker.role,
        sessionKind: "worker",
      });
    }
    return sessions.sort((left, right) => left.id.localeCompare(right.id));
  }, [agentsState.orchestrators, agentsState.workers]);
  const fleetGroups = useMemo(
    () => buildFleetGroups(agentsState.workers ?? [], agentsState.orchestrators),
    [agentsState.orchestrators, agentsState.workers]
  );
  const activeAgentWorker = agentTicket ? agentWorkers.get(agentTicket) ?? { ticket: agentTicket } : null;
  const modalOpen =
    switcherOpen || settingsOpen || windowChooserOpen || dialog !== null || contextMenu !== null;
  const windowChooserItems = useMemo(() => {
    const agentLocations = new Map<string, { windowId: string; paneId: string }>();
    for (const window of windowState.windows) {
      for (const pane of collectPaneInfos(window.layout)) {
        if (pane.ticket) agentLocations.set(pane.ticket, { windowId: window.id, paneId: pane.key });
      }
    }

    const items: FleetSwitcherItem[] = [];
    for (const group of fleetGroups) {
      const workers = group.items.filter((item) => item.kind === "worker");
      if (workers.length === 0) continue;
      items.push({
        key: `heading:${group.orch.id}`,
        value: group.orch.id,
        icon: <Bot size={14} />,
        label: group.orch.id,
        meta: group.meta,
        disabled: true,
      });
      for (const worker of workers) {
        const location = agentLocations.get(worker.ticket);
        const sourceWindow =
          location ? windowState.windows.find((window) => window.id === location.windowId) ?? null : null;
        items.push({
          key: `agent:${worker.ticket}`,
          value: worker.ticket,
          icon: <span className="fleet-switcher-glyph">{agentStateGlyph(worker.state, worker.live)}</span>,
          label: worker.ticket,
          meta: `${sourceWindow ? windowLabel(sourceWindow) : "not open"}${
            worker.detail ? ` · ${worker.detail}` : ""
          }`,
          indent: 1,
          active: focusedPaneTicket === worker.ticket,
          chooserKind: "agent",
          path: `agent://${worker.ticket}`,
          windowId: location?.windowId ?? null,
          paneId: location?.paneId ?? null,
        });
      }
    }

    const openNotes = windowState.windows.flatMap((window, index) =>
      collectPaneInfos(window.layout)
        .filter((pane): pane is PaneInfo & { kind: "note"; path: string } =>
          pane.kind === "note" && pane.path !== null
        )
        .map((pane) => ({ index, pane, window }))
    );
    if (openNotes.length > 0) {
      items.push({
        key: "heading:notes",
        value: "notes",
        icon: <BookOpen size={14} />,
        label: "open notes",
        meta: `${openNotes.length} panes`,
        disabled: true,
      });
      for (const { index, pane, window } of openNotes) {
        items.push({
          key: `note:${window.id}:${pane.key}`,
          value: pane.path,
          icon: <BookOpen size={14} />,
          label: basename(pane.path),
          meta: `${index}:${windowLabel(window)} · ${pane.path}`,
          indent: 1,
          active: activeWindow?.id === window.id && focusedPaneId === pane.key,
          chooserKind: "note",
          path: pane.path,
          windowId: window.id,
          paneId: pane.key,
        });
      }
    }
    return items;
  }, [activeWindow, fleetGroups, focusedPaneId, focusedPaneTicket, windowState.windows]);

  useEffect(() => {
    if (!focusedPaneId || !paneInfos.some((pane) => pane.key === focusedPaneId)) {
      const fallback = paneInfos[0]?.key ?? null;
      if (fallback) requestAnimationFrame(() => paneRefs.current.get(fallback)?.focus());
    }
  }, [focusedPaneId, paneInfos]);

  useEffect(() => {
    if (zoomedPaneId && !paneInfos.some((pane) => pane.key === zoomedPaneId)) {
      setZoomedPaneId(null);
    }
  }, [paneInfos, zoomedPaneId]);

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

  function findPaneLocationByTicket(ticket: string) {
    for (const window of windowState.windows) {
      const pane = collectPaneInfos(window.layout).find((candidate) => candidate.ticket === ticket);
      if (pane) return { pane, window };
    }
    return null;
  }

  function findPaneLocationByPath(path: string, preferredWindowId?: string | null) {
    return findPaneLocationByResource(path, undefined, preferredWindowId);
  }

  function findPaneLocationByResource(
    path: string,
    resourceKind?: ResourceKind,
    preferredWindowId?: string | null
  ) {
    const windows =
      preferredWindowId && windowState.windows.some((window) => window.id === preferredWindowId)
        ? [
            ...windowState.windows.filter((window) => window.id === preferredWindowId),
            ...windowState.windows.filter((window) => window.id !== preferredWindowId),
          ]
        : windowState.windows;
    for (const window of windows) {
      const pane = collectPaneInfos(window.layout).find(
        (candidate) => candidate.path === path && (resourceKind === undefined || candidate.resourceKind === resourceKind)
      );
      if (pane) return { pane, window };
    }
    return null;
  }

  function focusWindowPane(windowId: string, paneId: string) {
    preserveViewScrollRef.current = true;
    setWindowState((current) => {
      const target = current.windows.find((window) => window.id === windowId);
      if (current.activeWindowId === windowId && target?.focusedPaneId === paneId) {
        return current;
      }
      return normalizeWindowWorkspaceState({
        ...current,
        activeWindowId: windowId,
        windows: current.windows.map((window) =>
          window.id === windowId ? { ...window, focusedPaneId: paneId } : window
        ),
      });
    });
    requestAnimationFrame(() => paneRefs.current.get(paneId)?.focus());
  }

  function syncRouteToPath(
    path: string | null,
    options?: { panel?: AgentRoutePanel; resourceKind?: ResourceKind; syncHash?: boolean }
  ) {
    const syncHash = options?.syncHash ?? true;
    if (!path) {
      if (syncHash) navigate({ kind: "empty" });
      setError(null);
      setActiveNote(null);
      setAgentPanel(null);
      setAgentTicket(null);
      setTerminalRouteId(null);
      setMode("empty");
      return;
    }
    const ticket = ticketFromPanePath(path);
    if (ticket) {
      if (syncHash) navigate({ kind: "agent", panel: options?.panel ?? null, ticket });
      setError(null);
      setActiveNote(null);
      setAgentPanel(options?.panel ?? null);
      setAgentTicket(ticket);
      setTerminalRouteId(null);
      setMode("agent");
      return;
    }
    const terminalId = terminalIdFromPanePath(path);
    if (terminalId) {
      if (syncHash) navigate({ kind: "terminal", id: terminalId });
      setError(null);
      setActiveNote(null);
      setAgentPanel(null);
      setAgentTicket(null);
      setTerminalRouteId(terminalId);
      setMode("terminal");
      return;
    }
    const utility = utilityKindFromPanePath(path);
    if (utility) {
      showUtilityRoute(utility, { syncHash });
      return;
    }
    if (options?.resourceKind === "file") {
      if (syncHash) navigate({ kind: "file", path });
      setError(null);
      setActiveNote(null);
      setAgentPanel(null);
      setAgentTicket(null);
      setTerminalRouteId(null);
      setMode("file");
      return;
    }
    void showResourceRoute(path, "note", { syncHash });
  }

  function openPathInSoloWindow(path: string, resourceKind?: ResourceKind) {
    const windowId = nextWindowId();
    const paneId = nextPaneId();
    if (zoomedPaneId) setZoomedPaneId(null);
    setWindowState((current) =>
      normalizeWindowWorkspaceState({
        activeWindowId: windowId,
        windows: [...current.windows, createSoloWindow(windowId, paneId, path, resourceKind)],
      })
    );
    requestAnimationFrame(() => paneRefs.current.get(paneId)?.focus());
  }

  async function showResourceRoute(
    path: string,
    resourceKind: ResourceKind,
    options: { syncHash?: boolean; edit?: boolean } = {}
  ) {
    const { edit = false, syncHash = true } = options;
    if (resourceKind === "file") {
      if (syncHash) navigate({ kind: "file", path });
      setError(null);
      setActiveNote(null);
      setAgentTicket(null);
      setAgentPanel(null);
      setTerminalRouteId(null);
      setMode("file");
      return;
    }
    if (syncHash) navigate({ kind: edit ? "edit" : "note", path });
    setError(null);
    setAgentTicket(null);
    setAgentPanel(null);
    setTerminalRouteId(null);
    try {
      const note = await getNote(path);
      setActiveNote(note);
      if (edit) {
        setDraft({ title: note.title, path: note.path, content: note.content });
        setMode("edit");
      } else {
        setMode("view");
      }
    } catch (err) {
      if (err instanceof ApiError && (err.status === 404 || err.status === 410)) {
        forgetRecentResource("note", path);
      }
      setError(err instanceof Error ? err.message : "Could not open note");
    }
  }

  function forgetRecentResource(kind: RecentSwitcherItem["kind"], path: string, workspace = "wiki") {
    setRecentResources((current) =>
      current.filter((item) => !(item.kind === kind && item.workspace === workspace && item.path === path))
    );
  }

  function recentResourcePresence(
    kind: RecentSwitcherItem["kind"],
    path: string,
    workspace = "wiki"
  ): boolean | null {
    if (kind === "note") {
      return notesLoaded ? notes.some((note) => note.path === path) : null;
    }
    const file = parseFilePanePath(path);
    if (file) {
      workspace = file.workspace;
      path = file.path;
    }
    if (workspace !== activeWorkspace) return null;
    if (!showAllFiles || !filesLoaded || filesTruncated) return null;
    return files.some((file) => file.path === path);
  }

  async function openResource(
    path: string,
    resourceKind: ResourceKind,
    options: { focusExisting?: boolean; syncHash?: boolean; edit?: boolean; fromRecent?: boolean } = {}
  ) {
    const { edit = false, focusExisting = true, fromRecent = false, syncHash = true } = options;
    const file = resourceKind === "file" ? parseFilePanePath(path) : null;
    const recentWorkspace = file?.workspace ?? "wiki";
    const recentPath = file?.path ?? path;
    const presence = recentResourcePresence(resourceKind, path, recentWorkspace);
    if (fromRecent && presence === false) {
      forgetRecentResource(resourceKind, recentPath, recentWorkspace);
      return;
    }
    if (resourceKind === "note" || resourceKind === "file") {
      setRecentResources((current) => [
        { kind: resourceKind, workspace: recentWorkspace, path: recentPath },
        ...current.filter(
          (item) => !(item.kind === resourceKind && item.workspace === recentWorkspace && item.path === recentPath)
        ),
      ].slice(0, MAX_RECENT_RESOURCES));
    }
    const existing = focusExisting
      ? findPaneLocationByResource(path, resourceKind, activeWindow?.id ?? null)
      : null;
    if (existing) {
      if (zoomedPaneId && zoomedPaneId !== existing.pane.key) setZoomedPaneId(null);
      focusWindowPane(existing.window.id, existing.pane.key);
    } else {
      openPathInSoloWindow(path, resourceKind);
    }
    await showResourceRoute(path, resourceKind, { edit, syncHash });
  }

  async function openNote(
    path: string,
    options: { focusExisting?: boolean; syncHash?: boolean; edit?: boolean; fromRecent?: boolean } = {}
  ) {
    await openResource(path, "note", options);
  }

  async function openFile(
    path: string,
    options: { focusExisting?: boolean; syncHash?: boolean; fromRecent?: boolean } = {}
  ) {
    await openResource(normalizeFilePanePath(path), "file", options);
  }

  async function openRecentResource(item: RecentSwitcherItem) {
    if (item.kind === "file") {
      const panePath = filePanePath(item.workspace, item.path);
      if (recentResourcePresence("file", panePath, item.workspace) === false) {
        forgetRecentResource("file", item.path, item.workspace);
        return;
      }
      try {
        await getFileContent(item.workspace, item.path);
      } catch (error) {
        if (error instanceof ApiError && (error.status === 404 || error.status === 410)) {
          forgetRecentResource("file", item.path, item.workspace);
        }
        if (error instanceof Error) setError(error.message);
        else setError("Could not open file");
        return;
      }
    }
    if (item.kind === "file") {
      await openFile(filePanePath(item.workspace, item.path), { fromRecent: true });
    } else {
      await openNote(item.path, { fromRecent: true });
    }
  }

  function openTreeFile(file: TreeFile) {
    if (file.isNote) {
      void openNote(file.openPath);
    } else {
      void openFile(file.openPath);
    }
  }

  function showUtilityRoute(kind: UtilityMode, options: { syncHash?: boolean } = {}) {
    if (options.syncHash ?? true) navigate({ kind });
    setError(null);
    setActiveNote(null);
    setAgentTicket(null);
    setAgentPanel(null);
    setTerminalRouteId(null);
    setMode(kind);
  }

  function openUtilityView(kind: UtilityMode, options: { syncHash?: boolean } = {}) {
    const path = utilityPanePath(kind);
    const existing = findPaneLocationByPath(path, activeWindow?.id ?? null);
    if (existing) {
      if (zoomedPaneId && zoomedPaneId !== existing.pane.key) setZoomedPaneId(null);
      focusWindowPane(existing.window.id, existing.pane.key);
    } else {
      openPathInSoloWindow(path);
    }
    showUtilityRoute(kind, options);
  }

  function openAgent(ticket: string, panel: AgentRoutePanel = null, syncHash = true) {
    closedTicketsRef.current.delete(ticket);
    const existing = findPaneLocationByTicket(ticket);
    if (existing) {
      if (zoomedPaneId && zoomedPaneId !== existing.pane.key) setZoomedPaneId(null);
      focusWindowPane(existing.window.id, existing.pane.key);
    } else {
      const windowId = nextWindowId();
      const paneId = nextPaneId();
      if (zoomedPaneId) setZoomedPaneId(null);
      setWindowState((current) =>
        normalizeWindowWorkspaceState({
          activeWindowId: windowId,
          windows: [...current.windows, createSoloWindow(windowId, paneId, `agent://${ticket}`)],
        })
      );
      requestAnimationFrame(() => paneRefs.current.get(paneId)?.focus());
    }

    if (syncHash) navigate({ kind: "agent", panel, ticket });
    setError(null);
    setActiveNote(null);
    setAgentPanel(panel);
    setAgentTicket(ticket);
    setTerminalRouteId(null);
    setMode("agent");
  }

  function bumpTerminalLaunchNonce(terminalId: string) {
    setTerminalLaunchNonceById((current) => ({
      ...current,
      [terminalId]: (current[terminalId] ?? 0) + 1,
    }));
  }

  function openPathAsSplit(path: string, position: SplitPosition = "right") {
    if (!activeWindow || !focusedPaneId) {
      openPathInSoloWindow(path);
      return;
    }
    const newPaneId = nextPaneId();
    setWindowState((current) =>
      normalizeWindowWorkspaceState({
        activeWindowId: activeWindow.id,
        windows: current.windows.map((window) =>
          window.id === activeWindow.id
            ? {
                ...window,
                focusedPaneId: newPaneId,
                layout: splitLayout(window.layout, focusedPaneId, position, {
                  kind: "pane",
                  id: newPaneId,
                  path,
                }),
              }
            : window
        ),
      })
    );
    requestAnimationFrame(() => paneRefs.current.get(newPaneId)?.focus());
  }

  function createBlankPane() {
    const blankPaneId = nextPaneId();
    const blankPane: Extract<Layout, { kind: "pane" }> = {
      kind: "pane",
      id: blankPaneId,
      path: null,
    };

    if (!activeWindow || !focusedPaneId) {
      const windowId = nextWindowId();
      setWindowState((current) =>
        normalizeWindowWorkspaceState({
          activeWindowId: windowId,
          windows: [...current.windows, createSoloWindow(windowId, blankPaneId, null)],
        })
      );
    } else {
      setWindowState((current) =>
        normalizeWindowWorkspaceState({
          activeWindowId: activeWindow.id,
          windows: current.windows.map((window) =>
            window.id === activeWindow.id
              ? {
                  ...window,
                  focusedPaneId: blankPaneId,
                  layout: splitLayout(window.layout, focusedPaneId, "right", blankPane),
                }
              : window
          ),
        })
      );
    }
    if (zoomedPaneId) setZoomedPaneId(null);
    requestAnimationFrame(() => paneRefs.current.get(blankPaneId)?.focus());
    syncRouteToPath(null);
  }

  function moveSessionToFocusedBlankPane(ticket: string): boolean {
    if (!activeWindow || !focusedPaneId) return false;
    const target = findPaneInfo(activeWindow.layout, focusedPaneId);
    if (target?.kind !== "blank") return false;

    closedTicketsRef.current.delete(ticket);
    const source = findPaneLocationByTicket(ticket);
    if (!source) {
      const path = `agent://${ticket}`;
      setWindowState((current) =>
        normalizeWindowWorkspaceState({
          activeWindowId: activeWindow.id,
          windows: current.windows.map((window) =>
            window.id === activeWindow.id
              ? {
                  ...window,
                  focusedPaneId,
                  layout: replacePanePath(window.layout, focusedPaneId, path),
                }
              : window
          ),
        })
      );
      requestAnimationFrame(() => paneRefs.current.get(focusedPaneId)?.focus());
      syncRouteToPath(path);
      return true;
    }

    const nextWindows = windowState.windows.map((window) => ({ ...window }));
    const sourceIndex = nextWindows.findIndex((window) => window.id === source.window.id);
    if (sourceIndex < 0) return false;
    const removal = removePane(nextWindows[sourceIndex].layout, source.pane.key);
    if (removal.layout) nextWindows[sourceIndex].layout = removal.layout;
    else nextWindows.splice(sourceIndex, 1);

    const targetWindow = nextWindows.find((window) => window.id === activeWindow.id);
    if (!targetWindow) return false;
    targetWindow.layout = replacePane(targetWindow.layout, focusedPaneId, {
      kind: "pane",
      // Retain the source key so the session surface keeps its cached state.
      id: source.pane.key,
      path: source.pane.path,
      resourceKind: source.pane.resourceKind ?? undefined,
    });
    targetWindow.focusedPaneId = source.pane.key;

    const nextState = normalizeWindowWorkspaceState({
      activeWindowId: targetWindow.id,
      windows: nextWindows,
    });
    if (zoomedPaneId === focusedPaneId) setZoomedPaneId(source.pane.key);
    else if (zoomedPaneId && zoomedPaneId !== source.pane.key) setZoomedPaneId(null);
    setWindowState(nextState);
    requestAnimationFrame(() => paneRefs.current.get(source.pane.key)?.focus());
    syncRouteToPath(source.pane.path);
    return true;
  }

  function openSessionFromSwitcher(ticket: string) {
    if (!moveSessionToFocusedBlankPane(ticket)) openAgent(ticket);
  }

  function showTerminalRoute(
    terminalId: string,
    options: { launch?: boolean; splitIfNew?: boolean; syncHash?: boolean } = {}
  ) {
    const { launch = false, splitIfNew = false, syncHash = true } = options;
    const path = `terminal://${terminalId}`;
    const existing = findPaneLocationByPath(path, activeWindow?.id ?? null);
    if (existing) {
      if (zoomedPaneId && zoomedPaneId !== existing.pane.key) setZoomedPaneId(null);
      focusWindowPane(existing.window.id, existing.pane.key);
    } else if (splitIfNew) {
      openPathAsSplit(path, "right");
    } else {
      openPathInSoloWindow(path);
    }
    if (launch) bumpTerminalLaunchNonce(terminalId);
    if (syncHash) navigate({ kind: "terminal", id: terminalId });
    setError(null);
    setActiveNote(null);
    setAgentPanel(null);
    setAgentTicket(null);
    setTerminalRouteId(terminalId);
    setMode("terminal");
  }

  function createTerminalPane() {
    showTerminalRoute(crypto.randomUUID(), { launch: true, splitIfNew: true, syncHash: true });
  }

  function restartTerminalPane(terminalId: string) {
    showTerminalRoute(terminalId, { launch: true, splitIfNew: false, syncHash: true });
  }

  function registerTerminalController(
    terminalId: string,
    controller: TerminalPaneController | null
  ) {
    if (controller) terminalControllersRef.current.set(terminalId, controller);
    else terminalControllersRef.current.delete(terminalId);
  }

  function registerPaneRef(key: string, node: HTMLDivElement | null) {
    if (node) paneRefs.current.set(key, node);
    else paneRefs.current.delete(key);
  }

  function focusPane(key: string) {
    if (!activeWindow) return;
    focusWindowPane(activeWindow.id, key);
  }

  function activatePane(key: string) {
    if (!activeWindow) return;
    const pane = findPaneInfo(activeWindow.layout, key);
    if (!pane) return;
    if (zoomedPaneId && zoomedPaneId !== key) setZoomedPaneId(null);
    focusWindowPane(activeWindow.id, key);
    syncRouteToPath(pane.path, { resourceKind: pane.resourceKind ?? undefined });
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
    const notePreview = frame.querySelector<HTMLElement>(".markdown-preview-view");
    const scroller = notePreview?.closest<HTMLDivElement>(".view-content");
    if (!scroller) return false;
    scroller.scrollBy({ top: delta, behavior: "smooth" });
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
    leaderArmedRef.current = false;
    setLeaderArmed(false);
  }

  function armLeader() {
    if (leaderTimerRef.current) window.clearTimeout(leaderTimerRef.current);
    leaderArmedRef.current = true;
    setLeaderArmed(true);
    leaderTimerRef.current = window.setTimeout(() => {
      leaderTimerRef.current = null;
      leaderArmedRef.current = false;
      setLeaderArmed(false);
    }, 1500);
  }

  function executeLeaderChord(key: string, lowerKey: string) {
    if (key === "Escape") return;
    if (lowerKey === "a" && focusedTerminalId) {
      terminalControllersRef.current.get(focusedTerminalId)?.sendInput("\u0001");
      return;
    }
    if (lowerKey === "j") {
      cyclePaneFocus(1);
      return;
    }
    if (lowerKey === "k") {
      cyclePaneFocus(-1);
      return;
    }
    if (windowState.windows.length > 0 && lowerKey === "h") {
      activateWindowByIndex(
        activeWindowIndex >= 0
          ? (activeWindowIndex - 1 + windowState.windows.length) % windowState.windows.length
          : 0
      );
      return;
    }
    if (windowState.windows.length > 0 && lowerKey === "l") {
      activateWindowByIndex(
        activeWindowIndex >= 0
          ? (activeWindowIndex + 1) % windowState.windows.length
          : 0
      );
      return;
    }
    if (/^\d$/.test(key)) {
      const targetIndex = Number(key);
      if (targetIndex < windowState.windows.length) activateWindowByIndex(targetIndex);
      return;
    }
    if (lowerKey === "w") {
      setWindowChooserOpen(true);
      return;
    }
    if (lowerKey === "p") {
      createBlankPane();
      return;
    }
    if (lowerKey === "t") {
      createTerminalPane();
      return;
    }
    if (lowerKey === "x") {
      closeFocusedPane();
      return;
    }
    if (lowerKey === "z") {
      setZoomedPaneId((current) => (current === focusedPaneId ? null : focusedPaneId));
      if (focusedPaneId) focusPane(focusedPaneId);
      return;
    }
    if (key === ",") {
      setSettingsOpen(true);
    }
  }

  function cyclePaneFocus(delta: 1 | -1) {
    if (!activeWindow || !focusedPaneId || orderedPaneInfos.length === 0) return;
    const currentIndex = Math.max(
      0,
      orderedPaneInfos.findIndex((pane) => pane.key === focusedPaneId)
    );
    const nextPane =
      orderedPaneInfos[
        (currentIndex + delta + orderedPaneInfos.length) % orderedPaneInfos.length
      ];
    activatePane(nextPane.key);
  }

  function closeFocusedPane(targetPaneId: string | null = focusedPaneId) {
    if (!activeWindow || !targetPaneId) return;
    const focused = findPaneInfo(activeWindow.layout, targetPaneId);
    if (!focused) return;

    const nextWindows = windowState.windows.map((window) => ({ ...window }));
    const activeIndex = nextWindows.findIndex((window) => window.id === activeWindow.id);
    if (activeIndex < 0) return;

    const targetWindow = nextWindows[activeIndex];
    const panesInWindow = collectPaneInfos(targetWindow.layout);
    if (panesInWindow.length === 1) {
      nextWindows.splice(activeIndex, 1);
      if (focused.ticket) closedTicketsRef.current.add(focused.ticket);
    } else {
      const removal = removePane(targetWindow.layout, targetPaneId);
      if (!removal.layout) return;
      targetWindow.layout = removal.layout;
      if (focused.ticket) {
        nextWindows.push(createSoloWindow(nextWindowId(), nextPaneId(), focused.path, focused.resourceKind ?? undefined));
      }
    }

    const nextState = normalizeWindowWorkspaceState({
      activeWindowId:
        nextWindows[activeIndex]?.id ??
        nextWindows[Math.max(0, activeIndex - 1)]?.id ??
        null,
      windows: nextWindows,
    });
    if (zoomedPaneId === targetPaneId) setZoomedPaneId(null);
    clearSessionPaneState(targetPaneId);
    clearSurfacePaneState(targetPaneId);
    setWindowState(nextState);
    const nextActiveWindow =
      nextState.windows.find((window) => window.id === nextState.activeWindowId) ?? null;
    const nextPane = nextActiveWindow
      ? findPaneInfo(nextActiveWindow.layout, nextActiveWindow.focusedPaneId)
      : null;
    if (nextPane) {
      requestAnimationFrame(() => paneRefs.current.get(nextPane.key)?.focus());
    }
    syncRouteToPath(nextPane?.path ?? null, { resourceKind: nextPane?.resourceKind ?? undefined });
  }

  function activateWindowByIndex(index: number) {
    const target = windowState.windows[index];
    if (!target) return;
    if (zoomedPaneId && zoomedPaneId !== target.focusedPaneId) setZoomedPaneId(null);
    focusWindowPane(target.id, target.focusedPaneId);
    const pane = findPaneInfo(target.layout, target.focusedPaneId);
    syncRouteToPath(pane?.path ?? null, { resourceKind: pane?.resourceKind ?? undefined });
  }

  function moveChooserItemToFocusedPane(item: FleetSwitcherItem) {
    if (!item.path) return;
    if (item.windowId && item.paneId && activeWindow?.id === item.windowId) {
      if (zoomedPaneId && zoomedPaneId !== item.paneId) setZoomedPaneId(null);
      focusWindowPane(item.windowId, item.paneId);
      syncRouteToPath(item.path);
      return;
    }

    if (!activeWindow || !focusedPaneId) {
      if (item.windowId && item.paneId) {
        if (zoomedPaneId && zoomedPaneId !== item.paneId) setZoomedPaneId(null);
        focusWindowPane(item.windowId, item.paneId);
        syncRouteToPath(item.path);
        return;
      }
      if (item.chooserKind === "agent") {
        openAgent(item.value, null, true);
      } else {
        void openNote(item.path);
      }
      return;
    }

    const focused = findPaneInfo(activeWindow.layout, focusedPaneId);
    if (!focused) return;

    const nextWindows = windowState.windows.map((window) => ({ ...window }));
    if (item.windowId && item.paneId) {
      const sourceIndex = nextWindows.findIndex((window) => window.id === item.windowId);
      if (sourceIndex >= 0) {
        const removal = removePane(nextWindows[sourceIndex].layout, item.paneId);
        if (removal.layout) {
          nextWindows[sourceIndex].layout = removal.layout;
        } else {
          nextWindows.splice(sourceIndex, 1);
        }
        clearSessionPaneState(item.paneId);
        clearSurfacePaneState(item.paneId);
      }
    }

    const targetWindow = nextWindows.find((window) => window.id === activeWindow.id);
    if (!targetWindow) return;
    targetWindow.layout = replacePanePath(targetWindow.layout, focusedPaneId, item.path, "note");
    targetWindow.focusedPaneId = focusedPaneId;
    if (focused.path !== item.path && focused.ticket) {
      nextWindows.push(createSoloWindow(nextWindowId(), nextPaneId(), focused.path, focused.resourceKind ?? undefined));
      clearSessionPaneState(focusedPaneId);
      clearSurfacePaneState(focusedPaneId);
    }

    const nextState = normalizeWindowWorkspaceState({
      activeWindowId: targetWindow.id,
      windows: nextWindows,
    });
    if (zoomedPaneId && zoomedPaneId !== focusedPaneId) setZoomedPaneId(null);
    setWindowState(nextState);
    requestAnimationFrame(() => paneRefs.current.get(focusedPaneId)?.focus());
    syncRouteToPath(item.path, { resourceKind: "note" });
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
      setWindowState((current) =>
        normalizeWindowWorkspaceState({
          ...current,
          windows: current.windows.map((window) => ({
            ...window,
            layout: replaceMatchingPanePaths(window.layout, oldPath, result.path),
          })),
        })
      );
      setNotes(await listNotes());
      if (activeNote?.path === oldPath) void openNote(result.path);
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
          setWindowState((current) =>
            normalizeWindowWorkspaceState({
              ...current,
              windows: current.windows
                .map((window) => {
                  const layout = removePanePaths(window.layout, (candidate) => candidate === path);
                  if (!layout) return null;
                  const panes = collectPaneInfos(layout);
                  return {
                    ...window,
                    layout,
                    focusedPaneId: panes.some((pane) => pane.key === window.focusedPaneId)
                      ? window.focusedPaneId
                      : panes[0].key,
                  };
                })
                .filter((window): window is WorkspaceWindow => window !== null),
            })
          );
          setNotes(await listNotes());
          if (activeNote?.path === path) {
            navigate({ kind: "empty" });
            setActiveNote(null);
            setAgentTicket(null);
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
    const noteFolder = noteFolderFromTreePath(folderPath, showAllFiles);
    if (noteFolder === null) return;
    setDialog({
      title: "New note",
      input: noteFolder ? `${noteFolder}/` : "",
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
    const noteFolder = noteFolderFromTreePath(folderPath, showAllFiles);
    if (noteFolder === null) return;
    const dest = noteFolder ? `${noteFolder}/${basename(source)}.md` : `${basename(source)}.md`;
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
        await openNote(created.path, { focusExisting: false });
        return;
      }
      if (activeNote) {
        const updated = await updateNote(activeNote.path, draft.content);
        await refreshNotes(updated.path);
        await openNote(updated.path, { focusExisting: true });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save note");
    } finally {
      setIsSaving(false);
    }
  }

  const openAgentRef = useRef(openAgent);
  const openNoteRef = useRef(openNote);
  const openFileRef = useRef(openFile);
  const openUtilityViewRef = useRef(openUtilityView);
  const syncRouteToPathRef = useRef(syncRouteToPath);
  const activeWindowRef = useRef(activeWindow);
  openAgentRef.current = openAgent;
  openNoteRef.current = openNote;
  openFileRef.current = openFile;
  openUtilityViewRef.current = openUtilityView;
  syncRouteToPathRef.current = syncRouteToPath;
  activeWindowRef.current = activeWindow;

  useEffect(() => {
    let disposed = false;

    async function applyRoute() {
      const hash = window.location.hash || "#/";
      if (appliedHashRef.current === hash) return;
      appliedHashRef.current = hash;
      const route = parseRoute(hash);

      setError(null);
      if (route.kind === "empty") {
        const currentActive = activeWindowRef.current;
        const activePane = currentActive
          ? findPaneInfo(currentActive.layout, currentActive.focusedPaneId)
          : null;
        if (activePane) {
          syncRouteToPathRef.current(activePane.path, {
            resourceKind: activePane.resourceKind ?? undefined,
            syncHash: false,
          });
          return;
        }
        setActiveNote(null);
        setAgentPanel(null);
        setAgentTicket(null);
        setTerminalRouteId(null);
        setMode("empty");
        return;
      }
      if (route.kind === "new") {
        setActiveNote(null);
        setAgentPanel(null);
        setAgentTicket(null);
        setTerminalRouteId(null);
        setDraft(emptyDraft);
        setMode("new");
        return;
      }
      if (route.kind === "agent") {
        openAgentRef.current(route.ticket, route.panel, false);
        return;
      }
      if (route.kind === "terminal") {
        showTerminalRoute(route.id, { launch: false, splitIfNew: false, syncHash: false });
        return;
      }
      if (route.kind !== "note" && route.kind !== "edit" && route.kind !== "file") {
        openUtilityViewRef.current(route.kind, { syncHash: false });
        return;
      }

      if (route.kind === "file") {
        await openFileRef.current(route.path, { focusExisting: true, syncHash: false });
      } else {
        await openNoteRef.current(route.path, {
          edit: route.kind === "edit",
          focusExisting: true,
          syncHash: false,
        });
      }
      if (disposed) return;
    }

    applyRoute();
    window.addEventListener("hashchange", applyRoute);
    return () => {
      disposed = true;
      window.removeEventListener("hashchange", applyRoute);
    };
  }, []);

  useEffect(() => {
    function onLeaderKeyCapture(event: globalThis.KeyboardEvent) {
      const key = event.key;
      const lowerKey = key.toLowerCase();
      const modifierOnly = ["Shift", "Control", "Alt", "Meta"].includes(key);
      const terminalPane =
        event.target instanceof Element && event.target.closest("[data-terminal-pane='true']");

      if (terminalPane && (event.metaKey || event.ctrlKey) && lowerKey === "f") return;

      if (isTextEntryEvent(event)) return;

      if (leaderArmedRef.current) {
        if (modifierOnly) return;
        event.preventDefault();
        event.stopPropagation();
        disarmLeader();
        executeLeaderChord(key, lowerKey);
        return;
      }

      if (event.ctrlKey && !event.metaKey && !event.altKey && lowerKey === "a") {
        event.preventDefault();
        event.stopPropagation();
        armLeader();
      }
    }

    document.addEventListener("keydown", onLeaderKeyCapture, true);
    return () => document.removeEventListener("keydown", onLeaderKeyCapture, true);
  }, [
    activeWindowIndex,
    activateWindowByIndex,
    armLeader,
    closeFocusedPane,
    cyclePaneFocus,
    disarmLeader,
    executeLeaderChord,
    focusedPaneId,
    windowState.windows.length,
  ]);

  useEffect(() => {
    function onKeyDown(event: globalThis.KeyboardEvent) {
      if (event.defaultPrevented) return;

      const key = event.key;
      const lowerKey = key.toLowerCase();

      if (modalOpen || isTextEntryEvent(event)) return;
      if (
        document.activeElement instanceof HTMLTextAreaElement &&
        document.activeElement.dataset.terminalInput === "true"
      ) {
        return;
      }

      if ((event.metaKey || event.ctrlKey) && lowerKey === "k") {
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
    modalOpen,
    handlePaneScopeKey,
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
    agents: "Agents",
    tokens: "Tokens",
    dashboard: "Dashboard"
  };
  const themeToggleTarget = toggleThemePolarity(theme);
  const themeToggleTargetLabel = getTheme(themeToggleTarget).label;
  const currentThemeIsDark = isDarkTheme(theme);
  const tabTitle =
    mode === "new"
      ? "Untitled"
      : mode === "file"
        ? focusedPanePath?.split("/").pop() ?? "File"
      : mode === "agent"
        ? agentTicket ?? "Agent"
        : mode === "terminal"
          ? terminalRouteId ? `terminal:${terminalRouteId.slice(0, 8)}` : "Terminal"
        : utilityTitles[mode] ?? (activeNote ? basename(activeNote.path) : "New tab");
  const breadcrumbs =
    mode === "new"
      ? ["Untitled"]
      : mode === "file"
        ? focusedPanePath?.split("/") ?? []
      : mode === "agent"
        ? ["Agents", agentTicket ?? ""]
        : mode === "terminal"
          ? ["Terminal", terminalRouteId ?? ""]
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

  function handlePaneDrop(targetKey: string, zone: DropZone) {
    setZoomedPaneId(null);
    const path = draggingNotePath;
    setDraggingNotePath(null);
    if (!path) return;

    if (!activeWindow) {
      if (isAgentPath(path)) {
        openAgent(path.slice("agent://".length));
      } else {
        void openNote(path);
      }
      return;
    }

    if (zone === "center") {
      const existingAgent = isAgentPath(path)
        ? findPaneLocationByTicket(path.slice("agent://".length))
        : null;
      if (existingAgent?.window.id === activeWindow.id) {
        activatePane(existingAgent.pane.key);
        return;
      }

      const nextWindows = windowState.windows.map((window) => ({ ...window }));
      if (existingAgent) {
        const sourceIndex = nextWindows.findIndex((window) => window.id === existingAgent.window.id);
        if (sourceIndex >= 0) {
          const removal = removePane(nextWindows[sourceIndex].layout, existingAgent.pane.key);
          if (removal.layout) {
            nextWindows[sourceIndex].layout = removal.layout;
          } else {
            nextWindows.splice(sourceIndex, 1);
          }
          clearSessionPaneState(existingAgent.pane.key);
          clearSurfacePaneState(existingAgent.pane.key);
        }
      }

      const targetWindow = nextWindows.find((window) => window.id === activeWindow.id);
      const targetPane = targetWindow ? findPaneInfo(targetWindow.layout, targetKey) : null;
      if (!targetWindow || !targetPane) return;
      targetWindow.layout = replacePanePath(
        targetWindow.layout,
        targetKey,
        path,
        isAgentPath(path) ? undefined : "note"
      );
      targetWindow.focusedPaneId = targetKey;
      if (targetPane.path !== path && targetPane.ticket) {
        nextWindows.push(createSoloWindow(nextWindowId(), nextPaneId(), targetPane.path));
        clearSessionPaneState(targetKey);
        clearSurfacePaneState(targetKey);
      }
      setWindowState(
        normalizeWindowWorkspaceState({
          activeWindowId: targetWindow.id,
          windows: nextWindows,
        })
      );
      requestAnimationFrame(() => paneRefs.current.get(targetKey)?.focus());
      syncRouteToPath(path, { resourceKind: isAgentPath(path) ? undefined : "note" });
      return;
    }

    const existingAgent = isAgentPath(path)
      ? findPaneLocationByTicket(path.slice("agent://".length))
      : null;
    if (existingAgent?.window.id === activeWindow.id) {
      activatePane(existingAgent.pane.key);
      return;
    }

    const nextWindows = windowState.windows.map((window) => ({ ...window }));
    if (existingAgent) {
      const sourceIndex = nextWindows.findIndex((window) => window.id === existingAgent.window.id);
      if (sourceIndex >= 0) {
        const removal = removePane(nextWindows[sourceIndex].layout, existingAgent.pane.key);
        if (removal.layout) {
          nextWindows[sourceIndex].layout = removal.layout;
        } else {
          nextWindows.splice(sourceIndex, 1);
        }
        clearSessionPaneState(existingAgent.pane.key);
        clearSurfacePaneState(existingAgent.pane.key);
      }
    }

    const targetWindow = nextWindows.find((window) => window.id === activeWindow.id);
    if (!targetWindow) return;
    const newPaneId = nextPaneId();
    const newPane: Layout = {
      kind: "pane",
      id: newPaneId,
      path,
      resourceKind: isAgentPath(path) ? undefined : "note",
    };
    targetWindow.layout = splitLayout(targetWindow.layout, targetKey, zone, newPane);
    targetWindow.focusedPaneId = newPaneId;
    setWindowState(
      normalizeWindowWorkspaceState({
        activeWindowId: targetWindow.id,
        windows: nextWindows,
      })
    );
    requestAnimationFrame(() => paneRefs.current.get(newPaneId)?.focus());
    syncRouteToPath(path, { resourceKind: isAgentPath(path) ? undefined : "note" });
  }

  function renderPaneFrame(key: string, child: ReactNode) {
    return (
      <div
        data-pane-key={key}
        className={`pane-frame${focusedPaneId === key ? " is-focused" : ""}`}
        ref={(node) => registerPaneRef(key, node)}
        tabIndex={-1}
        onFocusCapture={() => {
          if (focusedPaneId !== key) activatePane(key);
        }}
        onMouseDownCapture={() => {
          if (focusedPaneId !== key) activatePane(key);
        }}
      >
        {child}
      </div>
    );
  }

  function renderFocusedPaneOverlayContent(): ReactNode {
    if (mode === "activity") {
      return <ActivityFeed onOpenNote={openNote} refreshTick={refreshTick} />;
    }
    if (mode === "graph") {
      return <GraphView onOpenNote={openNote} />;
    }
    if (mode === "health") {
      return <HealthView notes={notes} onOpenNote={openNote} />;
    }
    if (mode === "tokens") {
      return <TokensView />;
    }
    if (mode === "dashboard") {
      return <DashboardView />;
    }
    if (mode === "agents") {
      return (
        <AgentsView
          accountEvents={accountEvents}
          data={agentsState}
          onOpenAgent={openAgent}
          onOpenTicket={setAgentsOpenTicket}
          openTicket={agentsOpenTicket}
          refreshTick={refreshTick}
        />
      );
    }
    if (mode === "empty") {
      return (
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
      );
    }
    if (mode === "new") {
      return (
        <div className="markdown-source-view">
          <div className="markdown-sizer">
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
      );
    }
    return null;
  }

  function renderLayout(node: Layout, path: number[]): ReactNode {
    if (zoomedPaneId && node.kind === "split") {
      if (layoutContains(node.first, zoomedPaneId)) {
        return renderLayout(node.first, [...path, 1]);
      }
      if (layoutContains(node.second, zoomedPaneId)) {
        return renderLayout(node.second, [...path, 2]);
      }
    }

    if (node.kind === "pane") {
      const focused = focusedPaneId === node.id;
      const overlayContent = focused && node.path !== null ? renderFocusedPaneOverlayContent() : null;
      const agentContext =
        paneInfos.length === 1 || zoomedPaneId === node.id ? "full" : "pane";
      const noteFocusState: PaneNoteFocusState =
        focused && mode === "view" && activeNote?.path === node.path
          ? {
              kind: "view",
              links: links[activeNote.path] ?? null,
              note: activeNote,
              onChangeKanban: saveKanbanContent,
              onCompleteKanban: completeKanbanCard,
              onCreateNote: promptCreateUnresolved,
            }
          : focused && mode === "edit" && activeNote?.path === node.path
            ? {
                kind: "edit",
                draft,
                setDraft,
              }
            : null;
      const agentPanelForPane =
        focused && mode === "agent" && agentTicket && node.path === `agent://${agentTicket}`
          ? agentPanel
          : null;
      const scrollRef = focused && !overlayContent ? viewContentRef : undefined;
      return (
        <PaneDropTarget
          active={draggingNotePath !== null}
          key={node.id}
          onDropZone={(zone) => handlePaneDrop(node.id, zone)}
        >
          {renderPaneFrame(
            node.id,
            <WorkspacePane
              agentPanel={agentPanelForPane}
              agentContext={agentContext}
              agentWorkers={agentWorkers}
              focused={focused}
              noteFocusState={noteFocusState}
              notes={notes}
              onClose={() => closeFocusedPane(node.id)}
              onOpenNote={openNote}
              onRegisterTerminalController={registerTerminalController}
              onRestartTerminal={restartTerminalPane}
              overlayContent={overlayContent}
              paneStateKey={node.id}
              path={node.path}
              resourceKind={node.resourceKind}
              refreshTick={refreshTick}
              scrollRef={scrollRef}
              terminalLaunchNonce={
                terminalIdFromPanePath(node.path)
                  ? terminalLaunchNonceById[terminalIdFromPanePath(node.path) ?? ""] ?? 0
                  : 0
              }
            />
          )}
        </PaneDropTarget>
      );
    }
    return (
      <div
        className={`pane-split ${node.direction}`}
        key={path.join(".")}
        style={{ ["--split-ratio" as string]: node.ratio }}
      >
        <div className="pane-cell">
          {renderLayout(node.first, [...path, 1])}
        </div>
        <PaneDivider
          direction={node.direction}
          onRatio={(ratio) =>
            activeWindow &&
            setWindowState((current) =>
              normalizeWindowWorkspaceState({
                ...current,
                windows: current.windows.map((window) =>
                  window.id === activeWindow.id
                    ? { ...window, layout: setSplitRatio(window.layout, path, ratio) }
                    : window
                ),
              })
            )
          }
        />
        <div className="pane-cell">
          {renderLayout(node.second, [...path, 2])}
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
          aria-label="New terminal"
          className="ribbon-action"
          title="New terminal (C-a t)"
          type="button"
          onClick={createTerminalPane}
        >
          <TerminalIcon size={18} />
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
        <button
          aria-label="Token usage"
          className={`ribbon-action${mode === "tokens" ? " is-active" : ""}`}
          title="Token usage"
          type="button"
          onClick={() => openUtilityView("tokens")}
        >
          <TrendingUp size={18} />
        </button>
        <button
          aria-label="Ticket dashboard"
          className={`ribbon-action${mode === "dashboard" ? " is-active" : ""}`}
          title="Ticket dashboard"
          type="button"
          onClick={() => openUtilityView("dashboard")}
        >
          <ClipboardList size={18} />
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
                <button
                  aria-label={showAllFiles ? "Show notes only" : "Show all files"}
                  className={`nav-action-button${showAllFiles ? " is-active" : ""}`}
                  title={showAllFiles ? "Show notes only" : "Show all files"}
                  type="button"
                  onClick={() => setShowAllFiles((current) => !current)}
                >
                  <Files size={16} />
                </button>
              </div>
            </div>
            <label className="workspace-selector">
              <span>Workspace</span>
              <select
                aria-label="Files workspace"
                value={activeWorkspace}
                onChange={(event) => setActiveWorkspace(event.target.value)}
              >
                {(liveWorkspaces.length > 0 ? liveWorkspaces : [{ id: "wiki", root: "", live: true }]).map(
                  (workspace) => (
                    <option key={workspace.id} value={workspace.id}>
                      {workspace.id}
                    </option>
                  )
                )}
              </select>
            </label>
            <div className="nav-files-container">
              {isLoading || (showAllFiles && filesLoading) ? (
                <div className="nav-empty">
                  <LoadingPlaceholder className="nav-loading" lines={[92, 86, 88, 74, 81]} />
                </div>
              ) : tree.files.length > 0 || tree.folders.length > 0 ? (
                <>
                  <FolderTree
                    activePath={
                      mode === "file"
                        ? focusedPanePath
                        : mode === "view" || mode === "edit"
                          ? activeNote
                            ? showAllFiles
                              ? `vault/${activeNote.path}`
                              : activeNote.path
                            : null
                          : null
                    }
                    collapsed={collapsedFolders}
                    canDropOnFolder={(path) => noteFolderFromTreePath(path, showAllFiles) !== null}
                    depth={0}
                    dragActive={draggingNotePath !== null}
                    folder={tree}
                    onContextMenu={handleTreeContextMenu}
                    onDropOnFolder={handleDropOnFolder}
                    onNoteDragEnd={() => setDraggingNotePath(null)}
                    onNoteDragStart={setDraggingNotePath}
                    onOpenFile={openTreeFile}
                    onToggleFolder={toggleFolder}
                  />
                  {filesTruncated ? <div className="nav-empty">File list truncated at 10,000 items</div> : null}
                </>
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

        <div className={`view-header${mode === "agent" || mode === "terminal" ? " is-hidden" : ""}`}>
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
          {activeWindow ? (
            renderLayout(activeWindow.layout, [])
          ) : (
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
                />
              ) : mode === "tokens" ? (
                <TokensView />
              ) : mode === "dashboard" ? (
                <DashboardView />
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
              ) : null}
            </div>
          )}
        </div>

        <div className="status-bar">
          <div className="tmux-status">
            {windowState.windows.length > 0 ? (
              <div className="tmux-window-list">
                {windowState.windows.map((window, index) => (
                  <button
                    className={`tmux-status-item${index === activeWindowIndex ? " is-active" : ""}`}
                    key={window.id}
                    type="button"
                    onClick={() => activateWindowByIndex(index)}
                  >
                    <span className="tmux-status-index">{index}</span>
                    <span className="tmux-status-sep">:</span>
                    <span className="tmux-status-label">{windowLabel(window)}</span>
                  </button>
                ))}
              </div>
            ) : (
              <div className="tmux-group-empty">{agentsState.error ?? "No windows"}</div>
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
              disabled={noteFolderFromTreePath(contextMenu.path, showAllFiles) === null}
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
          files={files.map((file) => ({ workspace: activeWorkspace, path: file.path }))}
          filesLoading={filesLoading}
          notes={notes}
          recent={visibleRecentResources}
          sessions={quickSwitcherSessions}
          onClose={() => {
            setSwitcherOpen(false);
            requestAnimationFrame(() => focusedPaneId && paneRefs.current.get(focusedPaneId)?.focus());
          }}
          onOpen={(path) => {
            setSwitcherOpen(false);
            openNote(path);
          }}
          onOpenFile={(file) => {
            setSwitcherOpen(false);
            openFile(filePanePath(file.workspace, file.path));
          }}
          onOpenRecent={(item) => {
            setSwitcherOpen(false);
            void openRecentResource(item);
          }}
          onOpenSession={(ticket) => {
            setSwitcherOpen(false);
            openSessionFromSwitcher(ticket);
          }}
          onOpenPage={(page) => {
            setSwitcherOpen(false);
            openUtilityView(page);
          }}
        />
      ) : null}
      {windowChooserOpen ? (
        <FleetSwitcher
          items={windowChooserItems}
          title="Choose run or note pane"
          onClose={() => setWindowChooserOpen(false)}
          onPick={(item) => {
            setWindowChooserOpen(false);
            moveChooserItemToFocusedPane(item);
          }}
        />
      ) : null}
      {leaderArmed ? <div className="leader-indicator">C-a</div> : null}
    </div>
  );
}
