import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, ReactNode } from "react";
import { Bot, FileCode2, FileText, HeartPulse, History, Waypoints } from "lucide-react";
import type { NoteSummary } from "./types";

export type SwitcherPage = "graph" | "activity" | "health" | "agents";

export type QuickSwitcherSession = {
  id: string;
  model: string | null;
  orchestratorId: string | null;
  provider: string | null;
  role: string | null;
  sessionKind: "orchestrator" | "worker";
};

export type QuickSwitcherFile = {
  path: string;
};

export type RecentSwitcherItem = {
  kind: "note" | "file";
  path: string;
};

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
  | { kind: "file"; file: QuickSwitcherFile }
  | { kind: "recent"; recent: RecentSwitcherItem }
  | { kind: "page"; page: SwitcherPage; label: string }
  | { kind: "session"; session: QuickSwitcherSession };

type SwitcherGroup = {
  label: string;
  items: SwitcherItem[];
};

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

function matchScore(text: string, query: string): number | null {
  if (text === query) return 0;
  if (text.startsWith(query)) return 4;

  const segmentStart = text
    .split(/[\\/._\-\s]+/)
    .some((segment) => segment.startsWith(query));
  if (segmentStart) return 8;
  if (text.includes(query)) return 12 + text.indexOf(query) / Math.max(text.length, 1);

  const boundaries = new Set(["/", "\\", ".", "_", "-", " "]);
  const matchedIndexes: number[] = [];
  let queryIndex = 0;
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] !== query[queryIndex]) continue;
    matchedIndexes.push(index);
    queryIndex += 1;
    if (queryIndex === query.length) break;
  }
  if (queryIndex !== query.length) return null;

  const first = matchedIndexes[0] ?? 0;
  const last = matchedIndexes[matchedIndexes.length - 1] ?? first;
  const boundaryMatches = matchedIndexes.filter(
    (index) => index === 0 || boundaries.has(text[index - 1] ?? "")
  ).length;
  const gaps = last - first + 1 - query.length;
  return 40 + (query.length - boundaryMatches) * 4 + gaps + first / Math.max(text.length, 1);
}

function scorePath(path: string, query: string): number | null {
  const segments = path.split(/[\\/._\-\s]+/).filter(Boolean);
  const segmentScore = segments
    .map((segment) => matchScore(segment, query))
    .filter((score): score is number => score !== null)
    .sort((a, b) => a - b)[0];
  if (segmentScore !== undefined) return 10 + segmentScore;
  return matchScore(path, query);
}

function scoreNote(note: NoteSummary, query: string): number | null {
  const name = basename(note.path).toLowerCase();
  const path = note.path.toLowerCase();
  const title = note.title.toLowerCase();

  const titleScore = matchScore(title, query);
  if (titleScore !== null) return titleScore;
  const nameScore = scorePath(name, query);
  if (nameScore !== null) return 10 + nameScore;
  const pathScore = scorePath(path, query);
  return pathScore === null ? null : 20 + pathScore;
}

function scoreSession(session: QuickSwitcherSession, query: string): number | null {
  const fields = [
    session.id,
    session.role ?? "",
    session.model ?? "",
    session.orchestratorId ?? "",
    session.provider ?? "",
    session.sessionKind,
  ].map((field) => field.toLowerCase());

  if (fields.some((field) => field.startsWith(query))) return 0;
  if (fields.some((field) => field.includes(query))) return 1;
  if (fields.some((field) => matchScore(field, query) !== null)) return 2;
  return null;
}

function itemKey(item: SwitcherItem) {
  if (item.kind === "note") return `note:${item.note.id}`;
  if (item.kind === "file") return `file:${item.file.path}`;
  if (item.kind === "recent") return `recent:${item.recent.kind}:${item.recent.path}`;
  if (item.kind === "page") return `page:${item.page}`;
  return `session:${item.session.id}`;
}

function sessionMeta(session: QuickSwitcherSession) {
  return [session.role ?? session.sessionKind, session.model, session.provider]
    .filter((part): part is string => Boolean(part))
    .join(" · ");
}

export function QuickSwitcher({
  notes,
  files,
  recent,
  sessions,
  onClose,
  onOpen,
  onOpenFile,
  onOpenRecent,
  onOpenSession,
  onOpenPage
}: {
  notes: NoteSummary[];
  files: QuickSwitcherFile[];
  recent: RecentSwitcherItem[];
  sessions: QuickSwitcherSession[];
  onClose: () => void;
  onOpen: (path: string) => void;
  onOpenFile: (path: string) => void;
  onOpenRecent: (item: RecentSwitcherItem) => void;
  onOpenSession: (id: string) => void;
  onOpenPage: (page: SwitcherPage) => void;
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);

  const groups = useMemo<SwitcherGroup[]>(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return [
        {
          label: "Recent",
          items: recent.slice(0, 15).map((item): SwitcherItem => ({ kind: "recent", recent: item })),
        },
        {
          label: "Sessions",
          items: sessions.slice(0, 10).map((session): SwitcherItem => ({ kind: "session", session })),
        },
        {
          label: "Notes",
          items: notes.slice(0, 10).map((note): SwitcherItem => ({ kind: "note", note })),
        },
        {
          label: "Views",
          items: PAGES.map((entry): SwitcherItem => ({ kind: "page", ...entry })),
        },
      ].filter((group) => group.items.length > 0);
    }

    const sessionItems = sessions
      .map((session) => ({ session, score: scoreSession(session, needle) }))
      .filter((entry): entry is { session: QuickSwitcherSession; score: number } => entry.score !== null)
      .sort((a, b) => a.score - b.score || a.session.id.localeCompare(b.session.id))
      .slice(0, 10)
      .map((entry): SwitcherItem => ({ kind: "session", session: entry.session }));

    const noteItems = notes
      .map((note) => ({ note, score: scoreNote(note, needle) }))
      .filter((entry): entry is { note: NoteSummary; score: number } => entry.score !== null)
      .sort((a, b) => a.score - b.score || a.note.path.localeCompare(b.note.path))
      .slice(0, 10)
      .map((entry): SwitcherItem => ({ kind: "note", note: entry.note }));

    const notePaths = new Set(notes.map((note) => `vault/${note.path}`));
    const fileItems = files
      .filter((file) => !notePaths.has(file.path))
      .map((file) => ({ file, score: scorePath(file.path.toLowerCase(), needle) }))
      .filter((entry): entry is { file: QuickSwitcherFile; score: number } => entry.score !== null)
      .sort((a, b) => a.score - b.score || a.file.path.localeCompare(b.file.path))
      .slice(0, 10)
      .map((entry): SwitcherItem => ({ kind: "file", file: entry.file }));

    const pageItems = PAGES.filter((entry) =>
      entry.label.toLowerCase().includes(needle)
    ).map((entry): SwitcherItem => ({ kind: "page", ...entry }));

    return [
      { label: "Sessions", items: sessionItems },
      { label: "Notes", items: noteItems },
      { label: "Files", items: fileItems },
      { label: "Views", items: pageItems },
    ].filter((group) => group.items.length > 0);
  }, [files, notes, query, recent, sessions]);

  const results = useMemo(() => groups.flatMap((group) => group.items), [groups]);

  function activate(item: SwitcherItem) {
    if (item.kind === "note") {
      onOpen(item.note.path);
    } else if (item.kind === "file") {
      onOpenFile(item.file.path);
    } else if (item.kind === "recent") {
      onOpenRecent(item.recent);
    } else if (item.kind === "session") {
      onOpenSession(item.session.id);
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
          placeholder="Find a note, file, or session..."
          type="text"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={handleKeyDown}
        />
        <div className="quick-switcher-results">
          {results.length > 0 ? (
            groups.map((group) => (
              <div className="quick-switcher-group" key={group.label}>
                <div className="quick-switcher-group-label">{group.label}</div>
                {group.items.map((item) => {
                  const index = results.indexOf(item);
                  const Icon =
                    item.kind === "note"
                      ? FileText
                      : item.kind === "file"
                        ? FileCode2
                        : item.kind === "recent"
                          ? item.recent.kind === "file"
                            ? FileCode2
                            : FileText
                          : item.kind === "session"
                            ? Bot
                            : PAGE_ICONS[item.page];
                  return (
                    <button
                      className={`quick-switcher-result${index === selected ? " is-selected" : ""}`}
                      key={itemKey(item)}
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
                      ) : item.kind === "file" || item.kind === "recent" ? (
                        <>
                          <span className="quick-switcher-name">
                            {item.kind === "file"
                              ? item.file.path.split("/").pop() ?? item.file.path
                              : item.recent.path.split("/").pop() ?? item.recent.path}
                          </span>
                          <span className="quick-switcher-path">
                            {item.kind === "file" ? item.file.path : item.recent.path}
                          </span>
                        </>
                      ) : item.kind === "session" ? (
                        <>
                          <span className="quick-switcher-name">{item.session.id}</span>
                          <span className="quick-switcher-session-badge">{item.session.sessionKind}</span>
                          <span className="quick-switcher-path">{sessionMeta(item.session)}</span>
                        </>
                      ) : (
                        <>
                          <span className="quick-switcher-name">{item.label}</span>
                          <span className="quick-switcher-path">page</span>
                        </>
                      )}
                    </button>
                  );
                })}
              </div>
            ))
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
