import {
  lazy,
  Suspense,
  useEffect,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";
import { getLinks, getNote, updateNote, type NoteLinks } from "./api";
import { CodeFilePane } from "./code-file-pane";
import {
  AgentSessionSurface,
  type AgentRoutePanel,
  type AgentSessionSurfaceWorker,
} from "./agent-session-surface";
import { LoadingPlaceholder } from "./loading";
import { KanbanBoard, appendDoneEntry, type KanbanCard } from "./kanban";
import { ObsidianMarkdown, splitFrontmatter, stripLeadingTitle } from "./markdown";
import { TerminalPane, type TerminalPaneController } from "./terminal-pane";
import type { Note, NoteDraft, NoteSummary } from "./types";

const MarkdownSourceEditor = lazy(() => import("./source-editor"));

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

function SourceEditorPlaceholder({ content }: { content: string }) {
  return (
    <div className="source-editor-placeholder" aria-busy="true" aria-label="Loading source editor">
      <pre className="source-editor-placeholder-content">{content || "\u00a0"}</pre>
      <span className="source-editor-placeholder-status">Loading editor</span>
    </div>
  );
}

function NoteSourceEditor({
  content,
  focused,
  notePath,
  setDraft,
}: {
  content: string;
  focused: boolean;
  notePath: string;
  setDraft: Dispatch<SetStateAction<NoteDraft>>;
}) {
  return (
    <Suspense fallback={<SourceEditorPlaceholder content={content} />}>
      <MarkdownSourceEditor
        content={content}
        focused={focused}
        notePath={notePath}
        setDraft={setDraft}
      />
    </Suspense>
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
  agentContext,
  agentWorkers,
  focused,
  noteFocusState,
  notes,
  onClose,
  onOpenNote,
  onRegisterTerminalController,
  onTerminalNameChange,
  onRestartTerminal,
  overlayContent,
  paneStateKey,
  path,
  resourceKind,
  refreshTick,
  scrollRef,
  terminalName,
  terminalLaunchNonce,
}: {
  agentPanel: AgentRoutePanel;
  agentContext?: "full" | "pane";
  agentWorkers?: Map<string, AgentSessionSurfaceWorker>;
  focused: boolean;
  noteFocusState: PaneNoteFocusState;
  notes: NoteSummary[];
  onClose: () => void;
  onOpenNote: (path: string) => void;
  onRegisterTerminalController?: (terminalId: string, controller: TerminalPaneController | null) => void;
  onTerminalNameChange?: (terminalId: string, name: string | null) => void;
  onRestartTerminal?: (terminalId: string) => void;
  overlayContent?: ReactNode;
  paneStateKey: string;
  path: string | null;
  resourceKind?: "note" | "file";
  refreshTick: number;
  scrollRef?: RefObject<HTMLDivElement | null>;
  terminalName?: string | null;
  terminalLaunchNonce?: number;
}) {
  let content: ReactNode;
  if (path === null) {
    content = <BlankPane />;
  } else if (path.startsWith("agent://")) {
    content = (
      <AgentPane
        agentPanel={agentPanel}
        agentContext={agentContext ?? "pane"}
        agentWorkers={agentWorkers}
        onClose={onClose}
        paneStateKey={paneStateKey}
        path={path}
        refreshTick={refreshTick}
      />
    );
  } else if (path.startsWith("terminal://")) {
    const terminalId = path.slice("terminal://".length);
    content = (
      <TerminalPane
        focused={focused}
        launchNonce={terminalLaunchNonce ?? 0}
        onRegisterController={onRegisterTerminalController}
        onNameChange={onTerminalNameChange}
        onRestart={() => onRestartTerminal?.(terminalId)}
        customName={terminalName}
        terminalId={terminalId}
      />
    );
  } else if (path.startsWith("utility://")) {
    // Utility pages are provided by App's focused-pane overlay. Do not mount a
    // hidden note pane for their internal workspace identity.
    content = null;
  } else if (resourceKind === "file") {
    content = <CodeFilePane path={path} scrollRef={scrollRef} />;
  } else {
    content = (
      <NotePane
        focused={focused}
        focusState={noteFocusState}
        notes={notes}
        onOpenNote={onOpenNote}
        path={path}
        refreshTick={refreshTick}
        scrollRef={scrollRef}
      />
    );
  }

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

function BlankPane() {
  return (
    <section className="blank-pane" data-new-pane="true">
      <div className="blank-pane-title">New pane</div>
      <div className="blank-pane-hint">Cmd+K to move a live session here</div>
    </section>
  );
}

function AgentPane({
  agentPanel,
  agentContext,
  agentWorkers,
  path,
  paneStateKey,
  refreshTick,
  onClose,
}: {
  agentPanel: AgentRoutePanel;
  agentContext: "full" | "pane";
  agentWorkers?: Map<string, AgentSessionSurfaceWorker>;
  path: string;
  paneStateKey: string;
  refreshTick: number;
  onClose: () => void;
}) {
  const ticket = path.slice("agent://".length);
  const worker = agentWorkers?.get(ticket) ?? { ticket };

  return (
    <section className="secondary-pane agent-pane">
      <AgentSessionSurface
        key={path}
        context={agentContext}
        initialPanel={agentPanel}
        onClose={onClose}
        refreshTick={refreshTick}
        stateKey={paneStateKey}
        worker={worker}
      />
    </section>
  );
}

function NotePane({
  focused,
  focusState,
  notes,
  onOpenNote,
  path,
  refreshTick,
  scrollRef,
}: {
  focused: boolean;
  focusState: PaneNoteFocusState;
  notes: NoteSummary[];
  onOpenNote: (path: string) => void;
  path: string;
  refreshTick: number;
  scrollRef?: RefObject<HTMLDivElement | null>;
}) {
  const [note, setNote] = useState<Note | null>(null);
  const [localLinks, setLocalLinks] = useState<NoteLinks | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setNote(null);
    setLocalLinks(null);
    setError(null);
  }, [path]);

  useEffect(() => {
    if (focusState?.kind === "view") return;
    let ignore = false;
    getLinks()
      .then((all) => {
        if (!ignore) setLocalLinks(all[path] ?? null);
      })
      .catch(() => {
        if (!ignore) setLocalLinks(null);
      });
    return () => {
      ignore = true;
    };
  }, [path, refreshTick, focusState?.kind]);

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
  const backlinks =
    focusState?.kind === "view"
      ? focusState.links?.incoming ?? []
      : localLinks?.incoming ?? [];

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
      <div className="secondary-pane-content view-content" ref={scrollRef}>
        {error ? (
          <div className="notice" role="alert">
            <span>{error}</span>
          </div>
        ) : focusState?.kind === "edit" ? (
          <div className="markdown-source-view">
            <div className="markdown-sizer">
              <h1 className="inline-title">{focusState.draft.title}</h1>
              <NoteSourceEditor
                content={focusState.draft.content}
                focused={focused}
                notePath={path}
                setDraft={focusState.setDraft}
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
                    assetMeta={currentNote.asset_meta}
                    content={stripLeadingTitle(parsed.body, currentNote.title)}
                    notePath={currentNote.path}
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
