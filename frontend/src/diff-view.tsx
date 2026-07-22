import { Fragment, useMemo } from "react";

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

function stripPathPrefix(raw: string): string | null {
  const trimmed = raw.trim();
  if (!trimmed || trimmed === "/dev/null") return trimmed === "/dev/null" ? null : trimmed;
  const tab = trimmed.indexOf("\t");
  const value = tab === -1 ? trimmed : trimmed.slice(0, tab);
  if (value.startsWith("a/") || value.startsWith("b/")) return value.slice(2);
  return value;
}

function parseHunkHeader(header: string): { oldStart: number; newStart: number } {
  const match = /^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/.exec(header);
  if (!match) return { oldStart: 0, newStart: 0 };
  return { oldStart: Number(match[1]), newStart: Number(match[2]) };
}

export function parseUnifiedDiff(source: string): DiffFilePatch[] {
  if (!source) return [];
  const lines = source.split("\n");
  const files: DiffFilePatch[] = [];
  let current: DiffFilePatch | null = null;
  let currentHunk: DiffHunk | null = null;
  let oldCounter = 0;
  let newCounter = 0;

  function commitCurrent() {
    if (current) files.push(current);
    current = null;
    currentHunk = null;
  }

  function ensureFile(): DiffFilePatch {
    if (!current) {
      current = { oldPath: null, newPath: null, hunks: [], extendedHeaders: [] };
    }
    return current;
  }

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    if (line.startsWith(DIFF_GIT_MARKER)) {
      commitCurrent();
      current = { oldPath: null, newPath: null, hunks: [], extendedHeaders: [line] };
      currentHunk = null;
      continue;
    }
    if (line.startsWith(FILE_MARKER_OLD)) {
      const file = ensureFile();
      file.oldPath = stripPathPrefix(line.slice(FILE_MARKER_OLD.length));
      currentHunk = null;
      continue;
    }
    if (line.startsWith(FILE_MARKER_NEW)) {
      const file = ensureFile();
      file.newPath = stripPathPrefix(line.slice(FILE_MARKER_NEW.length));
      currentHunk = null;
      continue;
    }
    if (line.startsWith(HUNK_MARKER)) {
      const file = ensureFile();
      const bounds = parseHunkHeader(line);
      oldCounter = bounds.oldStart;
      newCounter = bounds.newStart;
      currentHunk = { header: line, lines: [] };
      file.hunks.push(currentHunk);
      continue;
    }
    if (!currentHunk) {
      if (current && (line.startsWith("index ") || line.startsWith("new file") || line.startsWith("deleted file") || line.startsWith("similarity ") || line.startsWith("rename ") || line.startsWith("copy ") || line.startsWith("Binary "))) {
        current.extendedHeaders.push(line);
      }
      continue;
    }
    if (line.startsWith("+")) {
      currentHunk.lines.push({
        kind: "add",
        text: line.slice(1),
        oldNumber: null,
        newNumber: newCounter,
      });
      newCounter += 1;
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
      continue;
    }
    if (line.startsWith("\\")) {
      currentHunk.lines.push({ kind: "meta", text: line, oldNumber: null, newNumber: null });
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
  }
  commitCurrent();
  return files;
}

function fileTitle(file: DiffFilePatch): string {
  const oldPath = file.oldPath;
  const newPath = file.newPath;
  if (oldPath && newPath && oldPath !== newPath) return `${oldPath} → ${newPath}`;
  return newPath || oldPath || "unnamed";
}

export function DiffPatchView({
  source,
  showLineNumbers = false,
  emptyMessage = "No diff to display.",
}: {
  source: string;
  showLineNumbers?: boolean;
  emptyMessage?: string;
}) {
  const files = useMemo(() => parseUnifiedDiff(source), [source]);
  if (files.length === 0 || files.every((file) => file.hunks.length === 0)) {
    return <div className="diff-view-empty">{emptyMessage}</div>;
  }
  return (
    <div
      className={`diff-view${showLineNumbers ? " has-line-numbers" : ""}`}
      data-line-numbers={showLineNumbers ? "true" : undefined}
    >
      {files.map((file, fileIndex) => (
        <div className="diff-file" key={`${fileTitle(file)}-${fileIndex}`}>
          <div className="diff-file-header">
            <span className="diff-file-path">{fileTitle(file)}</span>
          </div>
          <div className="diff-file-body">
            {file.hunks.map((hunk, hunkIndex) => (
              <Fragment key={`${hunk.header}-${hunkIndex}`}>
                <div className="diff-hunk-header" role="separator">
                  <span>{hunk.header}</span>
                </div>
                <div className="diff-hunk-body">
                  {hunk.lines.map((line, lineIndex) => {
                    const marker = line.kind === "add"
                      ? "+"
                      : line.kind === "remove"
                        ? "-"
                        : line.kind === "meta"
                          ? " "
                          : " ";
                    return (
                      <div className={`diff-line is-${line.kind}`} key={lineIndex}>
                        {showLineNumbers ? (
                          <>
                            <span className="diff-gutter diff-gutter-old tabular-nums">
                              {line.oldNumber ?? ""}
                            </span>
                            <span className="diff-gutter diff-gutter-new tabular-nums">
                              {line.newNumber ?? ""}
                            </span>
                          </>
                        ) : null}
                        <span className="diff-marker" aria-hidden="true">{marker}</span>
                        <span className="diff-code">{line.text}</span>
                      </div>
                    );
                  })}
                </div>
              </Fragment>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
