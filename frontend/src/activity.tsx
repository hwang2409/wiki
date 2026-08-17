import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, ChevronRight, FileText, RefreshCw } from "lucide-react";
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

function parseCommitDate(iso: string): Date | null {
  // git %aI keeps the author's UTC offset, so raw slice(0,10)/slice(11,16)
  // would bucket a commit under the author's calendar day/wall-clock, not
  // the viewer's. Parse once and derive both parts from the same local
  // Date. (Round-4 review LOW: offset-crossing commits landed under the
  // wrong local day/time.)
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return null;
  return new Date(t);
}

function dayOf(iso: string) {
  const d = parseCommitDate(iso);
  if (!d) return iso.slice(0, 10);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function timeOf(iso: string) {
  const d = parseCommitDate(iso);
  if (!d) return iso.slice(11, 16);
  const h = String(d.getHours()).padStart(2, "0");
  const min = String(d.getMinutes()).padStart(2, "0");
  return `${h}:${min}`;
}

const DAY_FORMATTER = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  month: "long",
  day: "numeric",
});

const DAY_FORMATTER_WITH_YEAR = new Intl.DateTimeFormat(undefined, {
  weekday: "long",
  month: "long",
  day: "numeric",
  year: "numeric",
});

function formatDayLabel(day: string, now: Date): string {
  // `day` is the YYYY-MM-DD prefix of the commit timestamp; the commits
  // come pre-sorted by ISO date, so parsing the naive date at midnight
  // local time is enough for the Today / Yesterday buckets.
  const parts = day.split("-").map(Number);
  if (parts.length !== 3 || parts.some(Number.isNaN)) return day;
  const bucket = new Date(parts[0], parts[1] - 1, parts[2]);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const diffMs = today.getTime() - bucket.getTime();
  const diffDays = Math.round(diffMs / (24 * 60 * 60 * 1000));
  if (diffDays === 0) return `Today · ${DAY_FORMATTER.format(bucket)}`;
  if (diffDays === 1) return `Yesterday · ${DAY_FORMATTER.format(bucket)}`;
  const formatter =
    bucket.getFullYear() === today.getFullYear() ? DAY_FORMATTER : DAY_FORMATTER_WITH_YEAR;
  return formatter.format(bucket);
}

const STATUS_LABELS: Record<string, string> = {
  A: "added",
  M: "modified",
  D: "deleted",
  R: "renamed",
  C: "copied",
  T: "type-changed",
  U: "unmerged",
};

function formatStatus(status: string): string {
  const key = (status ?? "").trim().slice(0, 1).toUpperCase();
  return STATUS_LABELS[key] ?? status ?? "changed";
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
    // Keep the last-loaded entries visible while the refetch is in
    // flight — clearing `commits` collapses the feed back to the empty /
    // loading state and defeats the stale-data banner. (Round-4 review
    // MEDIUM: retry destroyed the entries it was meant to preserve.)
    setError(null);
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

  const hasCommits = commits !== null && commits.length > 0;

  return (
    <UtilityPage
      title="Activity feed"
      subtitle="Chronological vault changes — each entry groups the notes touched together."
    >
      {error && !hasCommits ? (
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
        <>
          {error ? (
            <div
              className="activity-refresh-banner"
              role="status"
              aria-live="polite"
            >
              <AlertTriangle aria-hidden="true" size={13} />
              <span className="activity-refresh-message">
                Showing the last loaded entries. Refresh failed: {error}
              </span>
              <button
                className="activity-refresh-retry"
                type="button"
                onClick={retry}
              >
                <RefreshCw aria-hidden="true" size={12} />
                <span>Retry</span>
              </button>
            </div>
          ) : null}
          <ActivityBody
            commits={commits}
            expanded={expanded}
            onOpenNote={onOpenNote}
            onToggle={toggle}
          />
        </>
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
  const now = new Date();

  return (
    <div className="activity-feed">
      {byDay.map(({ day, commits: dayCommits }) => (
        <section className="activity-day" key={day}>
          <h2 className="activity-day-heading">{formatDayLabel(day, now)}</h2>
          {dayCommits.map((commit) => {
            const isOpen = expanded.has(commit.sha);
            const shortSha = commit.sha.slice(0, 7);
            return (
              <article className="activity-commit bb-detail-card" key={commit.sha}>
                <button
                  aria-expanded={isOpen}
                  className={`activity-commit-row session-activity-row session-tool${
                    isOpen ? " is-working" : ""
                  }`}
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
                        <span
                          className="activity-file-status"
                          title={`status: ${file.status}`}
                        >
                          {formatStatus(file.status)}
                        </span>
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
