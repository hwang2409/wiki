import { Fragment } from "react";
import { Diff, Hunk, parseDiff, type FileData } from "react-diff-view";

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
  const files = parseSplitDiff(patch);
  if (files.length === 0) {
    return <div className={emptyClassName}>{emptyMessage}</div>;
  }
  return (
    <div className={className}>
      {files.map((file, index) => (
        <div className="split-diff-file" key={`${fileHeader(file)}-${index}`}>
          <div className="split-diff-header">{fileHeader(file)}</div>
          <Diff
            className="wiki-diff"
            diffType={file.type}
            hunks={file.hunks}
            viewType={viewType}
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
      ))}
    </div>
  );
}
