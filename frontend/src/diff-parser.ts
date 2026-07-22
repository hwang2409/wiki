export type DiffLineKind = "add" | "remove" | "context" | "meta";

export type DiffLine = {
  kind: DiffLineKind;
  text: string;
  oldNumber: number | null;
  newNumber: number | null;
};

export type DiffHunk = {
  header: string;
  lines: DiffLine[];
};

export type DiffFileKind = "modified" | "new" | "deleted" | "renamed" | "binary" | "mode";

export type DiffFilePatch = {
  oldPath: string | null;
  newPath: string | null;
  hunks: DiffHunk[];
  extendedHeaders: string[];
};

const FILE_MARKER_OLD = "--- ";
const FILE_MARKER_NEW = "+++ ";
const HUNK_MARKER = "@@";
const DIFF_GIT_MARKER = "diff --git ";

const EXTENDED_HEADER_PREFIXES = [
  "index ",
  "new file",
  "deleted file",
  "old mode",
  "new mode",
  "similarity ",
  "dissimilarity ",
  "rename ",
  "copy ",
  "Binary ",
  "GIT binary patch",
];

export function stripPathPrefix(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed) return null;
  if (trimmed === "/dev/null") return null;
  const tab = trimmed.indexOf("\t");
  const value = tab === -1 ? trimmed : trimmed.slice(0, tab);
  if (value.startsWith("a/") || value.startsWith("b/")) return value.slice(2);
  return value;
}

function parseHunkHeader(header: string): {
  oldStart: number;
  oldCount: number;
  newStart: number;
  newCount: number;
} {
  const match = /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@/.exec(header);
  if (!match) return { oldStart: 0, oldCount: 0, newStart: 0, newCount: 0 };
  return {
    oldStart: Number(match[1]),
    oldCount: match[2] === undefined ? 1 : Number(match[2]),
    newStart: Number(match[3]),
    newCount: match[4] === undefined ? 1 : Number(match[4]),
  };
}

function isExtendedHeaderLine(line: string): boolean {
  return EXTENDED_HEADER_PREFIXES.some((prefix) => line.startsWith(prefix));
}

export function parseUnifiedDiff(source: string): DiffFilePatch[] {
  if (!source) return [];
  const lines = source.split("\n");
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();

  const files: DiffFilePatch[] = [];
  let current: DiffFilePatch | null = null;
  let currentHunk: DiffHunk | null = null;
  let oldCounter = 0;
  let newCounter = 0;
  let oldRemaining = 0;
  let newRemaining = 0;

  function commitCurrent() {
    if (current) files.push(current);
    current = null;
    currentHunk = null;
    oldRemaining = 0;
    newRemaining = 0;
  }

  function ensureFile(): DiffFilePatch {
    if (!current) {
      current = { oldPath: null, newPath: null, hunks: [], extendedHeaders: [] };
    }
    return current;
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];

    if (line.startsWith(HUNK_MARKER)) {
      const file = ensureFile();
      const bounds = parseHunkHeader(line);
      oldCounter = bounds.oldStart;
      newCounter = bounds.newStart;
      oldRemaining = bounds.oldCount;
      newRemaining = bounds.newCount;
      currentHunk = { header: line, lines: [] };
      file.hunks.push(currentHunk);
      continue;
    }

    if (line.startsWith(DIFF_GIT_MARKER)) {
      commitCurrent();
      current = { oldPath: null, newPath: null, hunks: [], extendedHeaders: [line] };
      continue;
    }

    if (currentHunk) {
      // Backslash metadata (`\ No newline at end of file`) belongs to the
      // preceding hunk line even when the hunk quota is exhausted.
      if (line.startsWith("\\")) {
        currentHunk.lines.push({ kind: "meta", text: line, oldNumber: null, newNumber: null });
        continue;
      }
      if (oldRemaining > 0 || newRemaining > 0) {
        if (line.startsWith("+")) {
          currentHunk.lines.push({
            kind: "add",
            text: line.slice(1),
            oldNumber: null,
            newNumber: newCounter,
          });
          newCounter += 1;
          newRemaining -= 1;
          continue;
        }
        if (line.startsWith("-")) {
          currentHunk.lines.push({
            kind: "remove",
            text: line.slice(1),
            oldNumber: oldCounter,
            newNumber: null,
          });
          oldCounter += 1;
          oldRemaining -= 1;
          continue;
        }
        const body = line.startsWith(" ") ? line.slice(1) : line;
        currentHunk.lines.push({
          kind: "context",
          text: body,
          oldNumber: oldCounter,
          newNumber: newCounter,
        });
        oldCounter += 1;
        newCounter += 1;
        oldRemaining -= 1;
        newRemaining -= 1;
        continue;
      }
      // Hunk quota consumed; hand off to file-boundary / header handling below.
      currentHunk = null;
    }

    if (line.startsWith(FILE_MARKER_OLD)) {
      if (current && (current.hunks.length > 0 || current.oldPath !== null || current.newPath !== null)) {
        commitCurrent();
      }
      const file = ensureFile();
      file.oldPath = stripPathPrefix(line.slice(FILE_MARKER_OLD.length));
      continue;
    }

    if (line.startsWith(FILE_MARKER_NEW)) {
      const file = ensureFile();
      file.newPath = stripPathPrefix(line.slice(FILE_MARKER_NEW.length));
      continue;
    }

    if (isExtendedHeaderLine(line)) {
      ensureFile().extendedHeaders.push(line);
      continue;
    }
    // Unknown pre-header lines (blank lines between files, prose) are ignored.
  }
  commitCurrent();
  return files;
}

export function fileTitle(file: DiffFilePatch): string {
  const oldPath = file.oldPath;
  const newPath = file.newPath;
  if (oldPath && newPath && oldPath !== newPath) return `${oldPath} → ${newPath}`;
  if (newPath) return newPath;
  if (oldPath) return oldPath;

  let renameFrom: string | null = null;
  let renameTo: string | null = null;
  for (const header of file.extendedHeaders) {
    if (header.startsWith("rename from ")) renameFrom = header.slice("rename from ".length).trim();
    if (header.startsWith("rename to ")) renameTo = header.slice("rename to ".length).trim();
  }
  if (renameFrom && renameTo) return `${renameFrom} → ${renameTo}`;

  for (const header of file.extendedHeaders) {
    if (header.startsWith(DIFF_GIT_MARKER)) {
      const rest = header.slice(DIFF_GIT_MARKER.length);
      const parts = rest.match(/^(.+?)\s+(.+)$/);
      if (parts) {
        const a = parts[1].startsWith("a/") ? parts[1].slice(2) : parts[1];
        const b = parts[2].startsWith("b/") ? parts[2].slice(2) : parts[2];
        return a === b ? a : `${a} → ${b}`;
      }
    }
  }

  for (const header of file.extendedHeaders) {
    if (header.startsWith("Binary files ")) {
      const match = /^Binary files (.+?) and (.+?) differ/.exec(header);
      if (match) {
        const a = match[1].startsWith("a/") ? match[1].slice(2) : match[1];
        const b = match[2].startsWith("b/") ? match[2].slice(2) : match[2];
        return a === b ? a : `${a} → ${b}`;
      }
    }
  }

  return "unnamed";
}

export function fileKind(file: DiffFilePatch): DiffFileKind | null {
  const headers = file.extendedHeaders;
  if (headers.some((header) => header.startsWith("deleted file"))) return "deleted";
  if (headers.some((header) => header.startsWith("new file"))) return "new";
  if (headers.some((header) => header.startsWith("Binary ") || header.startsWith("GIT binary patch"))) {
    return "binary";
  }
  if (headers.some((header) => header.startsWith("rename "))) return "renamed";
  if (headers.some((header) => header.startsWith("old mode") || header.startsWith("new mode"))) {
    return "mode";
  }
  return null;
}

const FILE_KIND_LABELS: Record<DiffFileKind, string> = {
  modified: "modified",
  new: "new file",
  deleted: "deleted",
  renamed: "renamed",
  binary: "binary",
  mode: "mode change",
};

export function fileKindLabel(kind: DiffFileKind): string {
  return FILE_KIND_LABELS[kind];
}
