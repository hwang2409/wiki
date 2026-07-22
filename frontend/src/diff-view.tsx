import { Fragment, useMemo } from "react";
import {
  fileKind,
  fileKindLabel,
  fileTitle,
  parseUnifiedDiff,
  type DiffFilePatch,
} from "./diff-parser";

export type {
  DiffFileKind,
  DiffFilePatch,
  DiffHunk,
  DiffLine,
  DiffLineKind,
} from "./diff-parser";
export { parseUnifiedDiff, fileTitle, fileKind, fileKindLabel } from "./diff-parser";

const HIDDEN_META_PREFIXES = ["diff --git ", "index "];

function extendedMetaLines(file: DiffFilePatch): string[] {
  return file.extendedHeaders.filter((line) =>
    !HIDDEN_META_PREFIXES.some((prefix) => line.startsWith(prefix)),
  );
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
  if (files.length === 0) {
    return <div className="diff-view-empty">{emptyMessage}</div>;
  }
  return (
    <div
      className={`diff-view${showLineNumbers ? " has-line-numbers" : ""}`}
      data-line-numbers={showLineNumbers ? "true" : undefined}
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
        );
      })}
    </div>
  );
}
