import {
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";
import { X } from "lucide-react";
import { getNote, updateNote, type NoteLinks } from "./api";
import {
  AgentSessionSurface,
  type AgentRoutePanel,
  type AgentSessionSurfaceWorker,
} from "./agent-session-surface";
import { LoadingPlaceholder } from "./loading";
import { KanbanBoard, appendDoneEntry, type KanbanCard } from "./kanban";
import { ObsidianMarkdown, splitFrontmatter, stripLeadingTitle } from "./markdown";
import type { Note, NoteDraft, NoteSummary } from "./types";

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function sameNote(left: Note | null, right: Note | null) {
  return (
    left?.path === right?.path &&
    left?.updated_at === right?.updated_at &&
    left?.content === right?.content
  );
}

export type PaneNoteFocusState =
  | {
      kind: "view";
      links: NoteLinks | null;
      note: Note;
      onChangeKanban: (next: string) => Promise<void> | void;
      onCompleteKanban: (card: KanbanCard) => Promise<void> | void;
      onCreateNote: (target: string) => void;
    }
  | {
      kind: "edit";
      draft: NoteDraft;
      setDraft: Dispatch<SetStateAction<NoteDraft>>;
    }
  | null;

export function WorkspacePane({
  agentPanel,
  agentWorkers,
  focused,
  noteFocusState,
  notes,
  onClose,
  onOpenNote,
  overlayContent,
  path,
  refreshTick,
  scrollRef,
}: {
  agentPanel: AgentRoutePanel;
  agentWorkers?: Map<string, AgentSessionSurfaceWorker>;
  focused: boolean;
  noteFocusState: PaneNoteFocusState;
  notes: NoteSummary[];
  onClose: () => void;
  onOpenNote: (path: string) => void;
  overlayContent?: ReactNode;
  path: string;
  refreshTick: number;
  scrollRef?: RefObject<HTMLDivElement | null>;
}) {
  const content = path.startsWith("agent://") ? (
    <AgentPane
      agentPanel={agentPanel}
      agentWorkers={agentWorkers}
      focused={focused}
      onClose={onClose}
      path={path}
      refreshTick={refreshTick}
    />
  ) : (
    <NotePane
      focused={focused}
      focusState={noteFocusState}
      notes={notes}
      onClose={onClose}
      onOpenNote={onOpenNote}
      path={path}
      refreshTick={refreshTick}
      scrollRef={scrollRef}
    />
  );

  return (
    <div className="pane-wrap">
      <div className="pane-wrap" style={{ display: overlayContent ? "none" : undefined }}>
        {content}
      </div>
      {overlayContent ? (
        <div className="view-content" ref={scrollRef}>
          {overlayContent}
        </div>
      ) : null}
    </div>
  );
}

function AgentPane({
  agentPanel,
  agentWorkers,
  focused,
  path,
  refreshTick,
  onClose,
}: {
  agentPanel: AgentRoutePanel;
  agentWorkers?: Map<string, AgentSessionSurfaceWorker>;
  focused: boolean;
  path: string;
  refreshTick: number;
  onClose: () => void;
}) {
  const ticket = path.slice("agent://".length);
  const worker = agentWorkers?.get(ticket) ?? { ticket };

  return (
    <section className="secondary-pane agent-pane">
      <AgentSessionSurface
        context={focused ? "full" : "pane"}
        initialPanel={focused ? agentPanel : null}
        onClose={focused ? undefined : onClose}
        refreshTick={refreshTick}
        worker={worker}
      />
    </section>
  );
}

function NotePane({
  focused,
  focusState,
  notes,
  onClose,
  onOpenNote,
  path,
  refreshTick,
  scrollRef,
}: {
  focused: boolean;
  focusState: PaneNoteFocusState;
  notes: NoteSummary[];
  onClose: () => void;
  onOpenNote: (path: string) => void;
  path: string;
  refreshTick: number;
  scrollRef?: RefObject<HTMLDivElement | null>;
}) {
  const [note, setNote] = useState<Note | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setNote(null);
    setError(null);
  }, [path]);

  useEffect(() => {
    if (focusState?.kind !== "view") return;
    setNote((previous) => (sameNote(previous, focusState.note) ? previous : focusState.note));
    setError(null);
  }, [focusState]);

  useEffect(() => {
    if (focusState) return;
    let ignore = false;

    getNote(path)
      .then((fresh) => {
        if (ignore) return;
        setNote((previous) => (sameNote(previous, fresh) ? previous : fresh));
        setError(null);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not open note");
      });

    return () => {
      ignore = true;
    };
  }, [path, refreshTick, focusState]);

  const currentNote = focusState?.kind === "view" ? focusState.note : note;
  const parsed = currentNote ? splitFrontmatter(currentNote.content) : null;
  const isKanban =
    parsed?.properties?.some(([key, value]) => key === "view" && value === "kanban") ?? false;
  const backlinks = focusState?.kind === "view" ? focusState.links?.incoming ?? [] : [];

  async function handleBoardChange(next: string) {
    setError(null);
    if (focusState?.kind === "view") {
      await focusState.onChangeKanban(next);
      return;
    }
    try {
      const updated = await updateNote(path, next);
      setNote(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save board change");
    }
  }

  async function handleComplete(card: KanbanCard) {
    setError(null);
    if (focusState?.kind === "view") {
      await focusState.onCompleteKanban(card);
      return;
    }
    if (!currentNote) return;
    try {
      const date = new Intl.DateTimeFormat("en-CA").format(new Date());
      const summary = card.text.replace(/^\[P\d\]\s*/, "");
      const done = await getNote("log/done.md");
      await updateNote("log/done.md", appendDoneEntry(done.content, summary, date));
      const lines = currentNote.content.split("\n");
      lines.splice(card.start, card.end - card.start + 1);
      const updated = await updateNote(path, lines.join("\n"));
      setNote(updated);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not complete card");
    }
  }

  return (
    <section className="secondary-pane" aria-label={`Pane: ${basename(path)}`}>
      {!focused ? (
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
      ) : null}
      <div className={`secondary-pane-content${focused ? " view-content" : ""}`} ref={scrollRef}>
        {error ? (
          <div className="notice" role="alert">
            <span>{error}</span>
          </div>
        ) : focusState?.kind === "edit" ? (
          <div className="markdown-source-view">
            <div className="markdown-sizer">
              <h1 className="inline-title">{focusState.draft.title}</h1>
              <textarea
                className="source-editor"
                spellCheck="true"
                value={focusState.draft.content}
                onChange={(event) =>
                  focusState.setDraft((current) => ({ ...current, content: event.target.value }))
                }
              />
            </div>
          </div>
        ) : currentNote && parsed ? (
          <div className="markdown-reading-view">
            <div className={`markdown-sizer${isKanban ? " kanban-sizer" : ""}`}>
              <h1 className="inline-title">{currentNote.title}</h1>
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
                  content={currentNote.content}
                  notes={notes}
                  onChange={handleBoardChange}
                  onComplete={handleComplete}
                  onOpenNote={onOpenNote}
                />
              ) : (
                <div className="markdown-preview-view">
                  <ObsidianMarkdown
                    content={stripLeadingTitle(parsed.body, currentNote.title)}
                    notes={notes}
                    onCreateNote={focusState?.kind === "view" ? focusState.onCreateNote : undefined}
                    onOpenNote={onOpenNote}
                  />
                </div>
              )}
              {backlinks.length > 0 ? (
                <div className="backlinks">
                  <div className="backlinks-heading">
                    Linked mentions
                    <span className="backlinks-count">{backlinks.length}</span>
                  </div>
                  <div className="backlinks-list">
                    {backlinks.map((linkPath) => (
                      <button
                        className="backlink"
                        key={linkPath}
                        type="button"
                        onClick={() => onOpenNote(linkPath)}
                      >
                        <span className="backlink-name">{basename(linkPath)}</span>
                        <span className="backlink-path">{linkPath}</span>
                      </button>
                    ))}
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        ) : (
          <div className="nav-empty">
            <LoadingPlaceholder className="note-loading" lines={[88, 96, 74, 84, 91]} />
          </div>
        )}
      </div>
    </section>
  );
}
