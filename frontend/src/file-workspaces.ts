import type { NoteSummary } from "./types";

export type FileReference = {
  workspace: string;
  path: string;
};

export type RecentResource = {
  kind: "note" | "file";
  workspace: string;
  path: string;
};

export type TreeFile = {
  path: string;
  openPath: string;
  id: string;
  isNote: boolean;
  workspace?: string;
};

export type TreeFolder = {
  name: string;
  path: string;
  folders: TreeFolder[];
  files: TreeFile[];
};

type WorkspaceStatus = {
  id: string;
  root: string;
  live: boolean;
};

export const MAX_RECENT_RESOURCES = 15;

export function filePanePath(workspace: string, path: string): string {
  return `file://${workspace}/${path}`;
}

export function workspaceFileSearchPath(workspace: string, path: string): string {
  return `${workspace}/${path}`;
}

export function parseFilePanePath(path: string): FileReference | null {
  const match = path.match(/^file:\/\/([^/]+)\/(.+)$/);
  return match ? { workspace: match[1], path: match[2] } : null;
}

export function normalizeFilePanePath(path: string): string {
  return parseFilePanePath(path) ? path : filePanePath("wiki", path);
}

export function isWorkspaceToken(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !/[\\/\x00]/.test(value);
}

export function fileResourceKey(workspace: string, path: string): string {
  return `${workspace}:${path}`;
}

export function isActiveFilePath(activePath: string | null, workspace: string, path: string): boolean {
  return activePath === filePanePath(workspace, path);
}

export function shouldDiscoverWorkspaces(
  sidebarTab: string,
  switcherOpen: boolean,
  agentsOpen = false,
): boolean {
  return sidebarTab === "files" || sidebarTab === "agents" || switcherOpen || agentsOpen;
}

export function workspaceCacheKey(workspace: WorkspaceStatus): string {
  return `${workspace.id}\u0000${workspace.root}`;
}

export function shouldAcceptWorkspaceResponse(
  requestVersion: number,
  currentVersion: number,
  workspace: WorkspaceStatus,
  cacheKey: string
): boolean {
  return requestVersion === currentVersion && workspace.live && workspaceCacheKey(workspace) === cacheKey;
}

export type WorkspaceRequest = {
  generation: number;
  token: symbol;
};

export class WorkspaceRequestTracker {
  private generation = 0;
  private pending = new Map<string, symbol>();

  invalidate(): void {
    this.generation += 1;
    this.pending.clear();
  }

  begin(key: string): WorkspaceRequest | null {
    if (this.pending.has(key)) return null;
    const request = { generation: this.generation, token: Symbol(key) };
    this.pending.set(key, request.token);
    return request;
  }

  isCurrent(key: string, request: WorkspaceRequest): boolean {
    return request.generation === this.generation && this.pending.get(key) === request.token;
  }

  finish(key: string, request: WorkspaceRequest): void {
    if (this.pending.get(key) === request.token) this.pending.delete(key);
  }
}

export function buildTree(
  notes: NoteSummary[],
  files?: Array<{ path: string }>,
  workspace = "wiki"
): TreeFolder {
  const repoNotePaths = new Set(notes.map((note) => `vault/${note.path}`));
  const treeFiles: Array<TreeFile> =
    files === undefined
      ? notes.map((note) => ({
          id: note.id,
          isNote: true,
          openPath: note.path,
          path: note.path,
        }))
      : [
          ...files
            .filter((file) => workspace !== "wiki" || !repoNotePaths.has(file.path))
            .map((file) => ({
              id: `${workspace}:${file.path}`,
              isNote: false,
              openPath: filePanePath(workspace, file.path),
              path: file.path,
              workspace,
            })),
          ...notes.map((note) => ({
            id: note.id,
            isNote: true,
            openPath: note.path,
            path: `vault/${note.path}`,
          })),
        ];
  const root: TreeFolder = { name: "", path: "", folders: [], files: [] };
  const folderIndex = new Map<string, TreeFolder>([["", root]]);

  for (const file of treeFiles) {
    const parts = file.path.split("/");
    let current = root;

    for (const part of parts.slice(0, -1)) {
      const folderPath = current.path ? `${current.path}/${part}` : part;
      let next = folderIndex.get(folderPath);
      if (!next) {
        next = { name: part, path: folderPath, folders: [], files: [] };
        folderIndex.set(folderPath, next);
        current.folders.push(next);
      }
      current = next;
    }

    current.files.push({
      id: file.id,
      isNote: file.isNote,
      openPath: file.openPath,
      path: file.path,
      workspace: file.workspace,
    });
  }

  const basename = (path: string) => path.split("/").pop()?.replace(/\.md$/, "") ?? path;
  const sortFolder = (folder: TreeFolder) => {
    folder.folders.sort((a, b) => a.name.localeCompare(b.name));
    folder.files.sort((a, b) => basename(a.path).localeCompare(basename(b.path)));
    folder.folders.forEach(sortFolder);
  };
  sortFolder(root);
  return root;
}

export function reconcileWorkspaceState<T>(
  workspaces: WorkspaceStatus[],
  activeWorkspace: string,
  cache: Record<string, T>
): { activeWorkspace: string; cache: Record<string, T> } {
  const knownIds = new Set(workspaces.map((workspace) => workspace.id));
  const liveCacheKeys = new Set(workspaces.filter((workspace) => workspace.live).map(workspaceCacheKey));
  const filteredCache = Object.fromEntries(
    Object.entries(cache).filter(([key]) => liveCacheKeys.has(key)),
  );
  // WIKI-151: always preserve the persisted selection so the UI can render it
  // as an explicit unavailable option instead of silently swapping the user
  // onto another root. When the id is unknown to discovery, the caller
  // synthesizes a `{live: false}` entry so the selector still shows the
  // persisted name with an "(unavailable)" marker rather than defaulting the
  // native <select> to whatever happens to be first.
  return { activeWorkspace, cache: filteredCache };
}

export function parseStoredRecentResources(raw: string | null): RecentResource[] {
  try {
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    if (!parsed || typeof parsed !== "object") return [];
    const stored = parsed as { v?: unknown; entries?: unknown };
    if ((stored.v !== 1 && stored.v !== 2) || !Array.isArray(stored.entries)) return [];
    const entries = stored.entries.map((item): RecentResource | null => {
      if (!item || typeof item !== "object") return null;
      const candidate = item as { kind?: unknown; path?: unknown; workspace?: unknown };
      if (
        (candidate.kind !== "note" && candidate.kind !== "file") ||
        typeof candidate.path !== "string" ||
        candidate.path.length === 0
      ) {
        return null;
      }
      const workspace = stored.v === 1 ? "wiki" : candidate.workspace;
      return isWorkspaceToken(workspace)
        ? { kind: candidate.kind, path: candidate.path, workspace }
        : null;
    });
    if (entries.some((item): item is null => item === null)) return [];
    const seen = new Set<string>();
    return (entries as RecentResource[])
      .filter((item) => {
        const key = `${item.kind}:${item.workspace}:${item.path}`;
        if (seen.has(key)) return false;
        seen.add(key);
        return true;
      })
      .slice(0, MAX_RECENT_RESOURCES);
  } catch {
    return [];
  }
}
