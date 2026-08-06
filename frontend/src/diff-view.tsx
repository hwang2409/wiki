import { Fragment, useMemo } from "react";
import {
  fileKind,
  fileKindLabel,
  fileTitle,
  parseUnifiedDiff,
  type DiffFilePatch,
  type DiffHunk,
  type DiffLine,
} from "./diff-parser";
import { TokenizedLine, languageForPath, useHighlightTokenLines, type TokenLine } from "./shiki";

export type {
  DiffFileKind,
  DiffFilePatch,
  DiffHunk,
  DiffLine,
  DiffLineKind,
} from "./diff-parser";
export { parseUnifiedDiff, fileTitle, fileKind, fileKindLabel } from "./diff-parser";

export type DiffViewType = "unified" | "split";

const HIDDEN_META_PREFIXES = ["diff --git ", "index "];

function extendedMetaLines(file: DiffFilePatch): string[] {
  return file.extendedHeaders.filter((line) =>
    !HIDDEN_META_PREFIXES.some((prefix) => line.startsWith(prefix)),
  );
}

// Token-level syntax inside add/remove/context lines, matching the file's
// language — the OpenCode edit-diff look (session/index.tsx:2404-2427). One
// tokenize call per hunk (interleaved old/new text tokenizes line-by-line);
// the shiki helper returns null over its size caps and the lines fall back
// to plain text, so bounded payloads stay bounded.
function DiffHunkBody({
  hunk,
  lang,
  showLineNumbers,
}: {
  hunk: DiffHunk;
  lang: string | null;
  showLineNumbers: boolean;
}) {
  const joined = useMemo(() => hunk.lines.map((line) => line.text).join("\n"), [hunk]);
  const tokenLines = useHighlightTokenLines(joined, lang);
  return (
    <div className="diff-hunk-body">
      {hunk.lines.map((line, lineIndex) => {
        const marker = line.kind === "add" ? "+" : line.kind === "remove" ? "-" : " ";
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
            <span className="diff-code">
              <TokenizedLine fallback={line.text} tokens={tokenLines?.[lineIndex]} />
            </span>
          </div>
        );
      })}
    </div>
  );
}

// Deletions on the LEFT, insertions on the RIGHT, context aligned on both
// sides, pure adds/removes get a hatched blank on the opposite side. Runs of
// removes and adds inside a hunk are paired positionally (Git's default
// side-by-side algorithm); if one side runs long the extras get blanks.
type SplitRow =
  | { kind: "pair"; left: DiffLine | null; right: DiffLine | null; leftIndex: number; rightIndex: number }
  | { kind: "meta"; text: string };

function pairHunkLines(hunk: DiffHunk): SplitRow[] {
  const rows: SplitRow[] = [];
  const lines = hunk.lines;
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.kind === "meta") {
      rows.push({ kind: "meta", text: line.text });
      i += 1;
      continue;
    }
    if (line.kind === "context") {
      rows.push({ kind: "pair", left: line, right: line, leftIndex: i, rightIndex: i });
      i += 1;
      continue;
    }
    const removes: { line: DiffLine; index: number }[] = [];
    while (i < lines.length && lines[i].kind === "remove") {
      removes.push({ line: lines[i], index: i });
      i += 1;
    }
    const adds: { line: DiffLine; index: number }[] = [];
    while (i < lines.length && lines[i].kind === "add") {
      adds.push({ line: lines[i], index: i });
      i += 1;
    }
    const pairLength = Math.max(removes.length, adds.length);
    for (let j = 0; j < pairLength; j += 1) {
      const left = removes[j];
      const right = adds[j];
      rows.push({
        kind: "pair",
        left: left?.line ?? null,
        right: right?.line ?? null,
        leftIndex: left?.index ?? -1,
        rightIndex: right?.index ?? -1,
      });
    }
  }
  return rows;
}

function SplitHunkBody({
  hunk,
  lang,
  showLineNumbers,
}: {
  hunk: DiffHunk;
  lang: string | null;
  showLineNumbers: boolean;
}) {
  const joined = useMemo(() => hunk.lines.map((line) => line.text).join("\n"), [hunk]);
  const tokenLines = useHighlightTokenLines(joined, lang);
  const rows = useMemo(() => pairHunkLines(hunk), [hunk]);
  return (
    <div className="diff-hunk-body is-split">
      {rows.map((row, rowIndex) => {
        if (row.kind === "meta") {
          return (
            <div className="diff-split-row is-meta" key={`meta-${rowIndex}`}>
              <span className="diff-split-meta">{row.text}</span>
            </div>
          );
        }
        return (
          <div className="diff-split-row" key={rowIndex}>
            <SplitCell
              fallback={row.left?.text ?? ""}
              gutterNumber={row.left?.oldNumber ?? null}
              kind={row.left ? row.left.kind : "blank"}
              marker={row.left ? (row.left.kind === "remove" ? "-" : " ") : ""}
              side="left"
              showLineNumbers={showLineNumbers}
              tokens={row.leftIndex >= 0 ? tokenLines?.[row.leftIndex] : null}
            />
            <SplitCell
              fallback={row.right?.text ?? ""}
              gutterNumber={row.right?.newNumber ?? null}
              kind={row.right ? row.right.kind : "blank"}
              marker={row.right ? (row.right.kind === "add" ? "+" : " ") : ""}
              side="right"
              showLineNumbers={showLineNumbers}
              tokens={row.rightIndex >= 0 ? tokenLines?.[row.rightIndex] : null}
            />
          </div>
        );
      })}
    </div>
  );
}

function SplitCell({
  fallback,
  gutterNumber,
  kind,
  marker,
  side,
  showLineNumbers,
  tokens,
}: {
  fallback: string;
  gutterNumber: number | null;
  kind: DiffLine["kind"] | "blank";
  marker: string;
  side: "left" | "right";
  showLineNumbers: boolean;
  tokens: TokenLine | null | undefined;
}) {
  return (
    <div className={`diff-line diff-split-cell is-${kind} is-side-${side}`}>
      {showLineNumbers ? (
        <span
          className={`diff-gutter diff-gutter-${side === "left" ? "old" : "new"} tabular-nums`}
        >
          {gutterNumber ?? ""}
        </span>
      ) : null}
      <span className="diff-marker" aria-hidden="true">{marker}</span>
      <span className="diff-code">
        {kind === "blank" ? "" : <TokenizedLine fallback={fallback} tokens={tokens} />}
      </span>
    </div>
  );
}

export function DiffPatchView({
  source,
  showLineNumbers = false,
  emptyMessage = "No diff to display.",
  viewType = "unified",
}: {
  source: string;
  showLineNumbers?: boolean;
  emptyMessage?: string;
  viewType?: DiffViewType;
}) {
  const files = useMemo(() => parseUnifiedDiff(source), [source]);
  if (files.length === 0) {
    return <div className="diff-view-empty">{emptyMessage}</div>;
  }
  return (
    <div
      className={`diff-view is-${viewType}${showLineNumbers ? " has-line-numbers" : ""}`}
      data-line-numbers={showLineNumbers ? "true" : undefined}
      data-view-type={viewType}
    >
      {files.map((file, fileIndex) => {
        const kind = fileKind(file);
        const metaLines = extendedMetaLines(file);
        const hasHunks = file.hunks.length > 0;
        return (
          <div className="diff-file" data-file-kind={kind ?? "modified"} key={`${fileTitle(file)}-${fileIndex}`}>
            <div className="diff-file-header">
              <span className="diff-file-path">{fileTitle(file)}</span>
              {kind ? <span className="diff-file-kind">{fileKindLabel(kind)}</span> : null}
            </div>
            <div className="diff-file-body">
              {!hasHunks && metaLines.length > 0 ? (
                <div className="diff-file-meta" role="note">
                  {metaLines.map((line, metaIndex) => (
                    <div className="diff-file-meta-line" key={`${line}-${metaIndex}`}>{line}</div>
                  ))}
                </div>
              ) : null}
              {file.hunks.map((hunk, hunkIndex) => (
                <Fragment key={`${hunk.header}-${hunkIndex}`}>
                  <div className="diff-hunk-header" role="separator">
                    <span>{hunk.header}</span>
                  </div>
                  {viewType === "split" ? (
                    <SplitHunkBody
                      hunk={hunk}
                      lang={languageForPath(fileTitle(file))}
                      showLineNumbers={showLineNumbers}
                    />
                  ) : (
                    <DiffHunkBody
                      hunk={hunk}
                      lang={languageForPath(fileTitle(file))}
                      showLineNumbers={showLineNumbers}
                    />
                  )}
                </Fragment>
              ))}
            </div>
          </div>
        );
      })}
    </div>
  );
}
