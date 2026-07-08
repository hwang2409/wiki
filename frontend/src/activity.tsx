import { useEffect, useState } from "react";
import { ChevronRight, FileText } from "lucide-react";
import { getActivity, getActivityDiff } from "./api";
import type { ActivityCommit } from "./api";
import { LoadingPlaceholder } from "./loading";
import { SplitDiffView } from "./split-diff";

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
  if (patch === null) {
    return (
      <div className="activity-diff-empty">
        <LoadingPlaceholder className="activity-loading" lines={[96, 84, 90]} />
      </div>
    );
  }
  return (
    <SplitDiffView
      className="activity-diff"
      emptyClassName="activity-diff-empty"
      emptyMessage="No vault changes in this commit."
      patch={patch}
    />
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
  if (commits === null) {
    return (
      <div className="activity-empty">
        <LoadingPlaceholder className="activity-loading" lines={[95, 86, 92, 78]} />
      </div>
    );
  }
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
                  <span className="activity-time tabular-nums">{timeOf(commit.date)}</span>
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
