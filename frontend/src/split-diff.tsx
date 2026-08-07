import { Fragment, useMemo } from "react";
import {
  Diff,
  Hunk,
  parseDiff,
  type FileData,
  type HunkTokens,
  type RenderToken,
  type TokenNode,
} from "react-diff-view";
import { languageForPath, languageFromContent, useHighlightTokenLines, type TokenLine } from "./shiki";

export type DiffFile = FileData;

export function parseSplitDiff(patch: string): DiffFile[] {
  try {
    return parseDiff(patch, { nearbySequences: "zip" });
  } catch {
    return [];
  }
}

function fileHeader(file: DiffFile): string {
  return file.newPath || file.oldPath || "unnamed";
}

type ShikiTokenNode = TokenNode & {
  type: "shiki";
  value: string;
  color?: string;
};

function shikiLineToNodes(line: TokenLine | undefined): TokenNode[] | undefined {
  if (!line) return undefined;
  return line.map<ShikiTokenNode>((token) => ({
    type: "shiki",
    value: token.content,
    color: token.color,
  }));
}

const renderShikiToken: RenderToken = (token, renderDefault, index) => {
  if (token.type === "shiki") {
    const { value, color } = token as ShikiTokenNode;
    return (
      <span key={index} style={color ? { color } : undefined}>
        {value}
      </span>
    );
  }
  return renderDefault(token, index);
};

// Build the per-file inputs Shiki needs to tokenize each side, mapped back to
// the file line numbers react-diff-view uses to index tokens.old / tokens.new.
// A single hunk is contiguous by definition, so the joined text preserves
// cross-line syntax context within one hunk; gaps between hunks are the same
// blind spot the built-in `tokenize` has.
// Language-detection sample: prefer new-side content (added lines + context)
// so a headerless codex diff still surfaces the file's real syntax shape.
// Bounded to keep the heuristic cheap regardless of hunk size.
function collectContentSample(hunks: FileData["hunks"]): string {
  const lines: string[] = [];
  let bytes = 0;
  const MAX = 4096;
  for (const hunk of hunks) {
    for (const change of hunk.changes) {
      if (change.type === "delete") continue;
      const text = change.content;
      lines.push(text);
      bytes += text.length + 1;
      if (bytes >= MAX) return lines.join("\n");
    }
  }
  return lines.join("\n");
}

function collectSideText(hunks: FileData["hunks"], side: "old" | "new") {
  const lines: Array<{ lineNumber: number; content: string }> = [];
  for (const hunk of hunks) {
    for (const change of hunk.changes) {
      if (change.type === "normal") {
        lines.push({
          lineNumber: side === "old" ? change.oldLineNumber : change.newLineNumber,
          content: change.content,
        });
      } else if (change.type === "delete" && side === "old") {
        lines.push({ lineNumber: change.lineNumber, content: change.content });
      } else if (change.type === "insert" && side === "new") {
        lines.push({ lineNumber: change.lineNumber, content: change.content });
      }
    }
  }
  lines.sort((a, b) => a.lineNumber - b.lineNumber);
  return lines;
}

function useSplitDiffTokens(file: FileData, lang: string | null): HunkTokens | null {
  const oldLines = useMemo(() => collectSideText(file.hunks, "old"), [file.hunks]);
  const newLines = useMemo(() => collectSideText(file.hunks, "new"), [file.hunks]);
  const oldText = useMemo(() => oldLines.map((entry) => entry.content).join("\n"), [oldLines]);
  const newText = useMemo(() => newLines.map((entry) => entry.content).join("\n"), [newLines]);
  const oldTokenLines = useHighlightTokenLines(oldText, lang);
  const newTokenLines = useHighlightTokenLines(newText, lang);

  return useMemo(() => {
    if (!oldTokenLines && !newTokenLines) return null;
    const oldSparse: TokenNode[][] = [];
    const newSparse: TokenNode[][] = [];
    if (oldTokenLines) {
      oldLines.forEach((entry, index) => {
        const nodes = shikiLineToNodes(oldTokenLines[index]);
        if (nodes) oldSparse[entry.lineNumber - 1] = nodes;
      });
    }
    if (newTokenLines) {
      newLines.forEach((entry, index) => {
        const nodes = shikiLineToNodes(newTokenLines[index]);
        if (nodes) newSparse[entry.lineNumber - 1] = nodes;
      });
    }
    return { old: oldSparse, new: newSparse };
  }, [oldLines, newLines, oldTokenLines, newTokenLines]);
}

// Extended: per-file component so we can call the shiki hooks once per file
// (React hook order stays stable regardless of how many files the patch has).
function SplitDiffFile({
  file,
  viewType,
}: {
  file: FileData;
  viewType: "split" | "unified";
}) {
  const path = fileHeader(file);
  const contentSample = useMemo(() => collectContentSample(file.hunks), [file.hunks]);
  const lang = useMemo(
    () => languageForPath(path) ?? languageFromContent(contentSample),
    [path, contentSample],
  );
  const tokens = useSplitDiffTokens(file, lang);
  return (
    <div className="split-diff-file">
      <div className="split-diff-header">{path}</div>
      <Diff
        className="wiki-diff"
        diffType={file.type}
        hunks={file.hunks}
        viewType={viewType}
        tokens={tokens}
        renderToken={renderShikiToken}
      >
        {(hunks) => (
          <Fragment>
            {hunks.map((hunk, hunkIndex) => (
              <Hunk hunk={hunk} key={`${hunk.content}-${hunkIndex}`} />
            ))}
          </Fragment>
        )}
      </Diff>
    </div>
  );
}

export function SplitDiffView({
  patch,
  className,
  emptyClassName,
  emptyMessage,
  viewType = "split",
}: {
  patch: string;
  className: string;
  emptyClassName: string;
  emptyMessage: string;
  viewType?: "split" | "unified";
}) {
  const files = useMemo(() => parseSplitDiff(patch), [patch]);
  if (files.length === 0) {
    return <div className={emptyClassName}>{emptyMessage}</div>;
  }
  return (
    <div className={className}>
      {files.map((file, index) => (
        <SplitDiffFile
          file={file}
          key={`${fileHeader(file)}-${index}`}
          viewType={viewType}
        />
      ))}
    </div>
  );
}
