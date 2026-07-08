import { Fragment } from "react";

type DiffRow =
  | { kind: "hunk"; text: string }
  | { kind: "context"; left: string; right: string }
  | { kind: "change"; left: string | null; right: string | null };

type DiffFile = {
  header: string;
  rows: DiffRow[];
};

export function parseSplitDiff(patch: string): DiffFile[] {
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let dels: string[] = [];
  let adds: string[] = [];

  const flush = () => {
    if (!current) return;
    const count = Math.max(dels.length, adds.length);
    for (let i = 0; i < count; i += 1) {
      current.rows.push({ kind: "change", left: dels[i] ?? null, right: adds[i] ?? null });
    }
    dels = [];
    adds = [];
  };

  for (const line of patch.split("\n")) {
    if (line.startsWith("diff ")) {
      flush();
      const match = line.match(/ b\/(.+)$/);
      current = { header: match?.[1] ?? line, rows: [] };
      files.push(current);
    } else if (!current) {
      continue;
    } else if (
      line.startsWith("+++") ||
      line.startsWith("---") ||
      line.startsWith("index ") ||
      line.startsWith("new file") ||
      line.startsWith("deleted file") ||
      line.startsWith("similarity ") ||
      line.startsWith("rename ")
    ) {
      continue;
    } else if (line.startsWith("@@")) {
      flush();
      current.rows.push({ kind: "hunk", text: line });
    } else if (line.startsWith("-")) {
      dels.push(line.slice(1));
    } else if (line.startsWith("+")) {
      adds.push(line.slice(1));
    } else {
      flush();
      const text = line.startsWith(" ") ? line.slice(1) : line;
      current.rows.push({ kind: "context", left: text, right: text });
    }
  }
  flush();
  return files;
}

export function SplitDiffView({
  patch,
  className,
  emptyClassName,
  emptyMessage,
}: {
  patch: string;
  className: string;
  emptyClassName: string;
  emptyMessage: string;
}) {
  const files = parseSplitDiff(patch);
  if (files.length === 0) {
    return <div className={emptyClassName}>{emptyMessage}</div>;
  }

  return (
    <div className={className}>
      {files.map((file) => (
        <div className="split-diff-file" key={file.header}>
          <div className="split-diff-header">{file.header}</div>
          <div className="split-diff-grid">
            {file.rows.map((row, index) =>
              row.kind === "hunk" ? (
                <div className="split-diff-hunk" key={index}>
                  {row.text}
                </div>
              ) : (
                <Fragment key={index}>
                  <div
                    className={`split-cell${
                      row.kind === "change"
                        ? row.left !== null
                          ? " is-del"
                          : " is-blank"
                        : ""
                    }`}
                  >
                    {row.left ?? " "}
                  </div>
                  <div
                    className={`split-cell split-cell-right${
                      row.kind === "change"
                        ? row.right !== null
                          ? " is-add"
                          : " is-blank"
                        : ""
                    }`}
                  >
                    {row.right ?? " "}
                  </div>
                </Fragment>
              )
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
