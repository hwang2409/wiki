import { useEffect, useMemo, useState } from "react";
import type { KeyboardEvent } from "react";
import { Bot, FileText, HeartPulse, History, Waypoints } from "lucide-react";
import type { NoteSummary } from "./types";

export type SwitcherPage = "graph" | "activity" | "health" | "agents";

type SwitcherItem =
  | { kind: "note"; note: NoteSummary }
  | { kind: "page"; page: SwitcherPage; label: string };

const PAGES: Array<{ page: SwitcherPage; label: string }> = [
  { page: "graph", label: "Graph" },
  { page: "activity", label: "Activity" },
  { page: "health", label: "Health" },
  { page: "agents", label: "Agents" }
];

const PAGE_ICONS = {
  graph: Waypoints,
  activity: History,
  health: HeartPulse,
  agents: Bot
} as const;

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function isSubsequence(needle: string, haystack: string) {
  let index = 0;
  for (const char of haystack) {
    if (char === needle[index]) index += 1;
    if (index === needle.length) return true;
  }
  return needle.length === 0;
}

function scoreNote(note: NoteSummary, query: string): number | null {
  const name = basename(note.path).toLowerCase();
  const path = note.path.toLowerCase();
  const title = note.title.toLowerCase();

  if (name.startsWith(query) || title.startsWith(query)) return 0;
  if (name.includes(query) || title.includes(query)) return 1;
  if (path.includes(query)) return 2;
  if (isSubsequence(query, path)) return 3;
  return null;
}

export function QuickSwitcher({
  notes,
  onClose,
  onOpen,
  onOpenPage
}: {
  notes: NoteSummary[];
  onClose: () => void;
  onOpen: (path: string) => void;
  onOpenPage: (page: SwitcherPage) => void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);

  const results = useMemo<SwitcherItem[]>(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return [
        ...notes.slice(0, 10).map((note): SwitcherItem => ({ kind: "note", note })),
        ...PAGES.map((entry): SwitcherItem => ({ kind: "page", ...entry }))
      ];
    }

    const noteItems = notes
      .map((note) => ({ note, score: scoreNote(note, needle) }))
      .filter((entry): entry is { note: NoteSummary; score: number } => entry.score !== null)
      .sort((a, b) => a.score - b.score || a.note.path.localeCompare(b.note.path))
      .map((entry): { item: SwitcherItem; score: number } => ({
        item: { kind: "note", note: entry.note },
        score: entry.score
      }));

    const pageItems = PAGES.filter((entry) =>
      entry.label.toLowerCase().includes(needle)
    ).map((entry): { item: SwitcherItem; score: number } => ({
      item: { kind: "page", ...entry },
      score: entry.label.toLowerCase().startsWith(needle) ? 0 : 1
    }));

    return [...noteItems, ...pageItems]
      .sort((a, b) => a.score - b.score)
      .slice(0, 10)
      .map((entry) => entry.item);
  }, [notes, query]);

  function activate(item: SwitcherItem) {
    if (item.kind === "note") {
      onOpen(item.note.path);
    } else {
      onOpenPage(item.page);
    }
  }

  useEffect(() => {
    setSelected(0);
  }, [query]);

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setSelected((index) => (results.length === 0 ? 0 : (index + 1) % results.length));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setSelected((index) =>
        results.length === 0 ? 0 : (index - 1 + results.length) % results.length
      );
    } else if (event.key === "Enter") {
      event.preventDefault();
      const target = results[selected];
      if (target) activate(target);
    } else if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        aria-label="Quick switcher"
        className="quick-switcher"
        role="dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <input
          autoFocus
          placeholder="Find a note..."
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={handleKeyDown}
        />
        <div className="quick-switcher-results">
          {results.length > 0 ? (
            results.map((item, index) => {
              const key = item.kind === "note" ? item.note.id : `page:${item.page}`;
              const Icon = item.kind === "note" ? FileText : PAGE_ICONS[item.page];
              return (
                <button
                  className={`quick-switcher-result${index === selected ? " is-selected" : ""}`}
                  key={key}
                  type="button"
                  onClick={() => activate(item)}
                  onMouseEnter={() => setSelected(index)}
                >
                  <Icon size={14} />
                  {item.kind === "note" ? (
                    <>
                      <span className="quick-switcher-name">{basename(item.note.path)}</span>
                      <span className="quick-switcher-path">{item.note.path}</span>
                    </>
                  ) : (
                    <>
                      <span className="quick-switcher-name">{item.label}</span>
                      <span className="quick-switcher-path">page</span>
                    </>
                  )}
                </button>
              );
            })
          ) : (
            <div className="quick-switcher-empty">No matches</div>
          )}
        </div>
      </div>
    </div>
  );
}
