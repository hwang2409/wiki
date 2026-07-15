export type FileReference = {
  workspace: string;
  path: string;
};

export type RecentResource = {
  kind: "note" | "file";
  workspace: string;
  path: string;
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
