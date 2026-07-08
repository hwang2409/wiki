import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { Bot, FileText, HeartPulse, History, Waypoints } from "lucide-react";
import type { NoteSummary } from "./types";

export type SwitcherPage = "graph" | "activity" | "health" | "agents";

export type FleetSwitcherItem = {
  key: string;
  value: string;
  icon?: ReactNode;
  label: string;
  meta: string;
  indent?: number;
  active?: boolean;
  disabled?: boolean;
  chooserKind?: "agent" | "note";
  path?: string;
  windowId?: string | null;
  paneId?: string | null;
};

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

export function FleetSwitcher({
  title,
  items,
  onClose,
  onPick,
}: {
  title: string;
  items: FleetSwitcherItem[];
  onClose: () => void;
  onPick: (item: FleetSwitcherItem) => void;
}) {
  const [selected, setSelected] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);

  function nextSelectableIndex(start: number, delta: 1 | -1) {
    if (items.length === 0) return 0;
    let index = start;
    for (let step = 0; step < items.length; step += 1) {
      index = (index + delta + items.length) % items.length;
      if (!items[index]?.disabled) return index;
    }
    return start;
  }

  useEffect(() => {
    rootRef.current?.focus();
  }, []);

  useEffect(() => {
    const activeIndex = items.findIndex((item) => item.active && !item.disabled);
    if (activeIndex >= 0) {
      setSelected(activeIndex);
      return;
    }
    const firstSelectable = items.findIndex((item) => !item.disabled);
    setSelected(firstSelectable >= 0 ? firstSelectable : 0);
  }, [items]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "ArrowDown" || event.key === "j") {
      event.preventDefault();
      setSelected((index) => nextSelectableIndex(index, 1));
      return;
    }
    if (event.key === "ArrowUp" || event.key === "k") {
      event.preventDefault();
      setSelected((index) => nextSelectableIndex(index, -1));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const item = items[selected];
      if (item && !item.disabled) onPick(item);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        aria-label={title}
        className="quick-switcher fleet-switcher"
        ref={rootRef}
        role="dialog"
        tabIndex={-1}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={handleKeyDown}
      >
        <div className="fleet-switcher-head">
          <span>{title}</span>
          <span className="fleet-switcher-count">{items.length} items</span>
        </div>
        <div className="quick-switcher-results">
          {items.length > 0 ? (
            items.map((item, index) => (
              <button
                className={`quick-switcher-result fleet-switcher-result${
                  index === selected ? " is-selected" : ""
                }${item.active ? " is-current" : ""}${item.disabled ? " is-disabled" : ""}`}
                disabled={item.disabled}
                key={item.key}
                style={{ paddingInlineStart: `${10 + (item.indent ?? 0) * 18}px` }}
                type="button"
                onClick={() => {
                  if (!item.disabled) onPick(item);
                }}
                onMouseEnter={() => {
                  if (!item.disabled) setSelected(index);
                }}
              >
                <span className="fleet-switcher-icon">{item.icon ?? <span />}</span>
                <span className="quick-switcher-name">{item.label}</span>
                <span className="quick-switcher-path">{item.meta}</span>
                {item.active ? <span className="fleet-switcher-current">active</span> : null}
              </button>
            ))
          ) : (
            <div className="quick-switcher-empty">No agents</div>
          )}
        </div>
      </div>
    </div>
  );
}
