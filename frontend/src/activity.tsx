import { Fragment, useEffect, useState } from "react";
import { ChevronRight, FileText } from "lucide-react";
import { getActivity, getActivityDiff } from "./api";
import type { ActivityCommit } from "./api";

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function vaultNotePath(filePath: string): string | null {
  if (!filePath.startsWith("vault/") || !filePath.endsWith(".md")) return null;
  return filePath.slice("vault/".length);
}

function dayOf(iso: string) {
  return iso.slice(0, 10);
}

function timeOf(iso: string) {
  return iso.slice(11, 16);
}

type DiffRow =
  | { kind: "hunk"; text: string }
  | { kind: "context"; left: string; right: string }
  | { kind: "change"; left: string | null; right: string | null };

type DiffFile = {
  header: string;
  rows: DiffRow[];
};

function parseSplitDiff(patch: string): DiffFile[] {
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

function DiffView({ sha }: { sha: string }) {
  const [patch, setPatch] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let ignore = false;
    getActivityDiff(sha)
      .then((result) => {
        if (!ignore) setPatch(result.patch);
      })
      .catch(() => {
        if (!ignore) setError(true);
      });
    return () => {
      ignore = true;
    };
  }, [sha]);

  if (error) return <div className="activity-diff-empty">Could not load diff.</div>;
  if (patch === null) return <div className="activity-diff-empty">Loading…</div>;

  const files = parseSplitDiff(patch);
  if (files.length === 0) {
    return <div className="activity-diff-empty">No vault changes in this commit.</div>;
  }

  return (
    <div className="activity-diff">
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

export function ActivityFeed({
  onOpenNote,
  refreshTick
}: {
  onOpenNote: (path: string) => void;
  refreshTick: number;
}) {
  const [commits, setCommits] = useState<ActivityCommit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    let ignore = false;

    getActivity(80)
      .then((next) => {
        if (ignore) return;
        setCommits((prev) =>
          prev && prev.length === next.length && prev[0]?.sha === next[0]?.sha ? prev : next
        );
        setError(null);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load activity");
      });

    return () => {
      ignore = true;
    };
  }, [refreshTick]);

  if (error) return <div className="activity-empty">{error}</div>;
  if (commits === null) return <div className="activity-empty">Loading…</div>;
  if (commits.length === 0) return <div className="activity-empty">No vault commits yet.</div>;

  const byDay: Array<{ day: string; commits: ActivityCommit[] }> = [];
  for (const commit of commits) {
    const day = dayOf(commit.date);
    const bucket = byDay[byDay.length - 1];
    if (bucket && bucket.day === day) {
      bucket.commits.push(commit);
    } else {
      byDay.push({ day, commits: [commit] });
    }
  }

  function toggle(sha: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(sha)) {
        next.delete(sha);
      } else {
        next.add(sha);
      }
      return next;
    });
  }

  return (
    <div className="activity-feed">
      {byDay.map(({ day, commits: dayCommits }) => (
        <section className="activity-day" key={day}>
          <h2 className="activity-day-heading">{day}</h2>
          {dayCommits.map((commit) => {
            const isOpen = expanded.has(commit.sha);
            return (
              <article className="activity-commit" key={commit.sha}>
                <button
                  className="activity-commit-row"
                  type="button"
                  onClick={() => toggle(commit.sha)}
                >
                  <ChevronRight
                    className={`collapse-icon${isOpen ? "" : " is-collapsed"}`}
                    size={14}
                  />
                  <span className="activity-time">{timeOf(commit.date)}</span>
                  <span className="activity-message">{commit.message}</span>
                </button>
                <div className="activity-files">
                  {commit.files.map((file) => {
                    const notePath = vaultNotePath(file.path);
                    return (
                      <button
                        className="activity-file"
                        disabled={!notePath}
                        key={file.path}
                        title={file.path}
                        type="button"
                        onClick={() => {
                          if (notePath) onOpenNote(notePath);
                        }}
                      >
                        <FileText size={11} />
                        <span>{basename(file.path)}</span>
                        <span className="activity-file-status">{file.status}</span>
                      </button>
                    );
                  })}
                </div>
                {isOpen ? <DiffView sha={commit.sha} /> : null}
              </article>
            );
          })}
        </section>
      ))}
    </div>
  );
}
