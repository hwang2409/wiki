import { useEffect, useState } from "react";
import { Bot, X } from "lucide-react";
import { getNote, updateNote } from "./api";
import { SessionTab, usePollTick } from "./session";
import { KanbanBoard, appendDoneEntry } from "./kanban";
import type { KanbanCard } from "./kanban";
import { ObsidianMarkdown, splitFrontmatter, stripLeadingTitle } from "./markdown";
import type { Note, NoteSummary } from "./types";

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

export function SecondaryPane({
  notes,
  onClose,
  onOpenNote,
  path,
  refreshTick
}: {
  notes: NoteSummary[];
  onClose: () => void;
  onOpenNote: (path: string) => void;
  path: string;
  refreshTick: number;
}) {
  if (path.startsWith("agent://")) {
    return (
      <AgentPane path={path} refreshTick={refreshTick} onClose={onClose} />
    );
  }
  return (
    <NotePane
      notes={notes}
      path={path}
      refreshTick={refreshTick}
      onClose={onClose}
      onOpenNote={onOpenNote}
    />
  );
}

function AgentPane({
  path,
  refreshTick,
  onClose,
}: {
  path: string;
  refreshTick: number;
  onClose: () => void;
}) {
  const ticket = path.slice("agent://".length);
  const tick = usePollTick(refreshTick);
  return (
    <section className="secondary-pane agent-pane">
      <header className="secondary-pane-header">
        <span className="secondary-pane-title">
          <Bot size={13} /> {ticket}
        </span>
        <button aria-label="Close pane" className="secondary-pane-close" type="button" onClick={onClose}>
          <X size={14} />
        </button>
      </header>
      <div className="agent-pane-body">
        <SessionTab ticket={ticket} tick={tick} />
      </div>
    </section>
  );
}

function NotePane({
  notes,
  onClose,
  onOpenNote,
  path,
  refreshTick
}: {
  notes: NoteSummary[];
  onClose: () => void;
  onOpenNote: (path: string) => void;
  path: string;
  refreshTick: number;
}) {
  const [note, setNote] = useState<Note | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setNote(null);
    setError(null);
  }, [path]);

  useEffect(() => {
    let ignore = false;

    getNote(path)
      .then((fresh) => {
        if (ignore) return;
        setNote((prev) =>
          prev && prev.path === fresh.path && prev.updated_at === fresh.updated_at &&
          prev.content === fresh.content
            ? prev
            : fresh
        );
        setError(null);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not open note");
      });

    return () => {
      ignore = true;
    };
  }, [path, refreshTick]);

  const parsed = note ? splitFrontmatter(note.content) : null;
  const isKanban =
    parsed?.properties?.some(([key, value]) => key === "view" && value === "kanban") ?? false;

  async function handleBoardChange(next: string) {
    try {
      const updated = await updateNote(path, next);
      setNote(updated);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save board change");
    }
  }

  async function handleComplete(card: KanbanCard) {
    if (!note) return;
    try {
      const date = new Intl.DateTimeFormat("en-CA").format(new Date());
      const summary = card.text.replace(/^\[P\d\]\s*/, "");
      const done = await getNote("log/done.md");
      await updateNote("log/done.md", appendDoneEntry(done.content, summary, date));
      const lines = note.content.split("\n");
      lines.splice(card.start, card.end - card.start + 1);
      const updated = await updateNote(path, lines.join("\n"));
      setNote(updated);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete card");
    }
  }

  return (
    <section className="secondary-pane" aria-label={`Pane: ${basename(path)}`}>
      <header className="secondary-pane-header">
        <span className="secondary-pane-title">{basename(path)}</span>
        <button
          aria-label="Close pane"
          className="view-action"
          title="Close pane"
          type="button"
          onClick={onClose}
        >
          <X size={14} />
        </button>
      </header>
      <div className="secondary-pane-content">
        {error ? (
          <div className="notice" role="alert">
            <span>{error}</span>
          </div>
        ) : note && parsed ? (
          <div className="markdown-reading-view" key={note.path}>
            <div className={`markdown-sizer${isKanban ? " kanban-sizer" : ""}`}>
              <h1 className="inline-title">{note.title}</h1>
              {!isKanban && parsed.properties && parsed.properties.length > 0 ? (
                <div className="metadata-container" aria-label="Properties">
                  {parsed.properties.map(([key, value]) => (
                    <div className="metadata-property" key={key}>
                      <span className="metadata-property-key">{key}</span>
                      <span className="metadata-property-value">
                        {Array.isArray(value)
                          ? value.map((item) => (
                              <span className="metadata-pill" key={item}>
                                {item}
                              </span>
                            ))
                          : value}
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
              {isKanban ? (
                <KanbanBoard
                  content={note.content}
                  notes={notes}
                  onChange={handleBoardChange}
                  onComplete={handleComplete}
                  onOpenNote={onOpenNote}
                />
              ) : (
                <div className="markdown-preview-view">
                  <ObsidianMarkdown
                    content={stripLeadingTitle(parsed.body, note.title)}
                    notes={notes}
                    onOpenNote={onOpenNote}
                  />
                </div>
              )}
            </div>
          </div>
        ) : (
          <div className="nav-empty">Loading...</div>
        )}
      </div>
    </section>
  );
}
