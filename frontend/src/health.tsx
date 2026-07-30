import { useMemo, useState } from "react";
import { FileText } from "lucide-react";
import type { NoteSummary } from "./types";
import { UtilityEmpty, UtilityPage } from "./utility-page";

const LIVING_TYPES = new Set(["reference", "campaign"]);
const DAY_MS = 24 * 60 * 60 * 1000;

type Bucket = "fresh" | "aging" | "stale";

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function ageDays(note: NoteSummary): number {
  const raw = note.meta_updated || note.updated_at;
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
  onOpenNote,
}: {
  notes: NoteSummary[];
  onOpenNote: (path: string) => void;
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

  return (
    <UtilityPage
      title="Vault health"
      subtitle={
        <>
          Living notes ({showAll ? "all types" : "reference + campaign"}) ranked stalest-first —
          agent memory rots when these stop moving.
        </>
      }
      actions={
        <label className="health-toggle">
          <input
            checked={showAll}
            type="checkbox"
            onChange={(event) => setShowAll(event.target.checked)}
          />
          <span>all types</span>
        </label>
      }
    >
      <div className="health-view">
        <div className="health-summary" role="group" aria-label="Vault freshness summary">
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
                  : "No living notes yet"
            }
            message={
              notes.length === 0
                ? "Create a note and it will appear here as it ages."
                : showAll
                  ? "Try creating a note or toggle types."
                  : "Reference and campaign notes power agent memory — toggle all types to widen this list."
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
          Structural vault drift is tracked separately — the note surface stays clean regardless.
        </p>
      </div>
    </UtilityPage>
  );
}
