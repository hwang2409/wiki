import { useMemo, useState } from "react";
import { FileText } from "lucide-react";
import type { NoteSummary } from "./types";
import { UtilityEmpty, UtilityError, UtilityLoading, UtilityPage } from "./utility-page";

const LIVING_TYPES = new Set(["reference", "campaign"]);
const DAY_MS = 24 * 60 * 60 * 1000;

type Bucket = "fresh" | "aging" | "stale";

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

const DATE_ONLY_RE = /^\d{4}-\d{2}-\d{2}$/;

export function parseNoteUpdated(raw: string): number {
  // Vault frontmatter `updated: YYYY-MM-DD` is a CALENDAR date, not a
  // UTC instant. Date.parse("2026-07-30") returns midnight UTC — which
  // is the previous day in any negative-offset locale, so a note edited
  // today shows as "1d" and the whole 7d/30d bucketing shifts early.
  // Parse date-only values as local midnight instead. (Round-4 review
  // MEDIUM: negative-offset viewers saw same-day notes as one day old.)
  if (DATE_ONLY_RE.test(raw)) {
    const [y, m, d] = raw.split("-").map(Number);
    return new Date(y, m - 1, d).getTime();
  }
  return Date.parse(raw);
}

// Calendar-day distance between two local Dates. Normalizing through
// Date.UTC using each Date's local Y/M/D coordinates strips the
// wall-clock time and any DST-induced hour skew: a spring-forward day
// is 23 real hours and a fall-back day is 25, so dividing raw elapsed
// ms by a fixed 24h drifts by ±1 day across the transition (round-5
// review MEDIUM). Full timestamps still use elapsed ms because those
// values are instants, not calendar dates.
export function calendarDayDiff(from: Date, to: Date): number {
  const utcFrom = Date.UTC(from.getFullYear(), from.getMonth(), from.getDate());
  const utcTo = Date.UTC(to.getFullYear(), to.getMonth(), to.getDate());
  return Math.floor((utcTo - utcFrom) / DAY_MS);
}

function ageDays(note: NoteSummary): number {
  const raw = note.meta_updated || note.updated_at;
  if (DATE_ONLY_RE.test(raw)) {
    const [y, m, d] = raw.split("-").map(Number);
    return Math.max(0, calendarDayDiff(new Date(y, m - 1, d), new Date()));
  }
  const time = Date.parse(raw);
  if (Number.isNaN(time)) return 0;
  return Math.max(0, Math.floor((Date.now() - time) / DAY_MS));
}

function bucketOf(days: number): Bucket {
  if (days < 7) return "fresh";
  if (days <= 30) return "aging";
  return "stale";
}

const BUCKET_LABEL: Record<Bucket, string> = {
  fresh: "fresh (<7d)",
  aging: "aging (7–30d)",
  stale: "stale (>30d)",
};

export function HealthView({
  notes,
  notesLoaded,
  loading,
  error,
  onOpenNote,
  onRetry,
}: {
  notes: NoteSummary[];
  notesLoaded: boolean;
  loading: boolean;
  error: string | null;
  onOpenNote: (path: string) => void;
  onRetry: () => void;
}) {
  const [showAll, setShowAll] = useState(false);

  const rows = useMemo(() => {
    return notes
      .filter((note) => showAll || LIVING_TYPES.has(note.note_type ?? ""))
      .map((note) => ({ note, days: ageDays(note) }))
      .sort((a, b) => b.days - a.days);
  }, [notes, showAll]);

  const counts = useMemo(() => {
    const result: Record<Bucket, number> = { fresh: 0, aging: 0, stale: 0 };
    for (const row of rows) result[bucketOf(row.days)] += 1;
    return result;
  }, [rows]);

  const showToggle = notesLoaded && !error;

  return (
    <UtilityPage
      title="Note freshness"
      subtitle={
        <>
          {showAll ? "All notes" : "Reference and campaign notes"} ranked oldest-first, so
          long-lived notes stay easy to spot and revisit.
        </>
      }
      actions={
        showToggle ? (
          <label className="health-toggle">
            <input
              checked={showAll}
              type="checkbox"
              onChange={(event) => setShowAll(event.target.checked)}
            />
            <span>all types</span>
          </label>
        ) : null
      }
    >
      {error ? (
        <UtilityError
          title="Note freshness is unavailable"
          message={error}
          onRetry={onRetry}
        />
      ) : loading ? (
        <UtilityLoading label="Reading vault notes…" />
      ) : (
        <div className="health-view">
          <div className="health-summary" role="group" aria-label="Note freshness summary">
            {(Object.keys(counts) as Bucket[]).map((bucket) => (
              <div className={`health-stat health-${bucket}`} key={bucket}>
                <span className="health-stat-count tabular-nums">{counts[bucket]}</span>
                <span className="health-stat-label">{BUCKET_LABEL[bucket]}</span>
              </div>
            ))}
          </div>

          {rows.length === 0 ? (
            <UtilityEmpty
              title={
                notes.length === 0
                  ? "No notes in the vault yet"
                  : showAll
                    ? "No notes match this view"
                    : "No reference or campaign notes yet"
              }
              message={
                notes.length === 0
                  ? "Create a note and it will appear here as it ages."
                  : showAll
                    ? "Try creating a note or toggle types."
                    : "Toggle all types to include shorter-lived notes in this list."
              }
            />
          ) : (
            <div className="health-list">
              {rows.map(({ note, days }) => (
                <button
                  className={`health-row is-${bucketOf(days)}`}
                  key={note.id}
                  type="button"
                  onClick={() => onOpenNote(note.path)}
                >
                  <FileText size={13} />
                  <span className="health-name">{basename(note.path)}</span>
                  <span className="health-type">{note.note_type ?? "—"}</span>
                  <span className="health-path">{note.path}</span>
                  <span className="health-age tabular-nums">
                    {days === 0 ? "today" : `${days}d`}
                  </span>
                </button>
              ))}
            </div>
          )}

          <p className="health-secondary-hint">
            Structural checks live elsewhere — this view only tracks how recently each note was
            edited.
          </p>
        </div>
      )}
    </UtilityPage>
  );
}
