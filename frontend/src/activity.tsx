import { useCallback, useEffect, useState } from "react";
import { ChevronRight, FileText } from "lucide-react";
import { getActivity, getActivityDiff } from "./api";
import type { ActivityCommit } from "./api";
import { LoadingPlaceholder } from "./loading";
import { SplitDiffView } from "./split-diff";
import { UtilityEmpty, UtilityError, UtilityLoading, UtilityPage } from "./utility-page";

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

  if (error) return <div className="activity-diff-empty">Could not load changes.</div>;
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
      emptyMessage="No vault changes in this entry."
      patch={patch}
    />
  );
}

export function ActivityFeed({
  onOpenNote,
  refreshTick,
}: {
  onOpenNote: (path: string) => void;
  refreshTick: number;
}) {
  const [commits, setCommits] = useState<ActivityCommit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [retryTick, setRetryTick] = useState(0);

  useEffect(() => {
    let ignore = false;

    getActivity(80)
      .then((next) => {
        if (ignore) return;
        setCommits((prev) =>
          prev && prev.length === next.length && prev[0]?.sha === next[0]?.sha ? prev : next,
        );
        setError(null);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load activity");
      });

    return () => {
      ignore = true;
    };
  }, [refreshTick, retryTick]);

  const retry = useCallback(() => {
    setError(null);
    setCommits(null);
    setRetryTick((tick) => tick + 1);
  }, []);

  const toggle = useCallback((sha: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(sha)) next.delete(sha);
      else next.add(sha);
      return next;
    });
  }, []);

  return (
    <UtilityPage
      title="Activity feed"
      subtitle="Chronological vault changes — each entry groups the notes touched together."
    >
      {error ? (
        <UtilityError
          message={error}
          onRetry={retry}
          title="Activity is unavailable"
        />
      ) : commits === null ? (
        <UtilityLoading label="Reading vault activity…" />
      ) : commits.length === 0 ? (
        <UtilityEmpty
          title="No vault activity yet"
          message="Vault changes will land here as notes are created, edited, or moved."
        />
      ) : (
        <ActivityBody
          commits={commits}
          expanded={expanded}
          onOpenNote={onOpenNote}
          onToggle={toggle}
        />
      )}
    </UtilityPage>
  );
}

function ActivityBody({
  commits,
  expanded,
  onOpenNote,
  onToggle,
}: {
  commits: ActivityCommit[];
  expanded: Set<string>;
  onOpenNote: (path: string) => void;
  onToggle: (sha: string) => void;
}) {
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

  return (
    <div className="activity-feed">
      {byDay.map(({ day, commits: dayCommits }) => (
        <section className="activity-day" key={day}>
          <h2 className="activity-day-heading">{day}</h2>
          {dayCommits.map((commit) => {
            const isOpen = expanded.has(commit.sha);
            const shortSha = commit.sha.slice(0, 7);
            return (
              <article className="activity-commit" key={commit.sha}>
                <button
                  aria-expanded={isOpen}
                  className="activity-commit-row"
                  type="button"
                  onClick={() => onToggle(commit.sha)}
                >
                  <ChevronRight
                    className={`collapse-icon${isOpen ? "" : " is-collapsed"}`}
                    size={14}
                  />
                  <span className="activity-time tabular-nums">{timeOf(commit.date)}</span>
                  <span className="activity-message">{commit.message}</span>
                  <span
                    className="activity-sha tabular-nums"
                    title={`revision ${commit.sha}`}
                  >
                    {shortSha}
                  </span>
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
