import {
  useEffect,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
  type RefObject,
  type SetStateAction,
} from "react";
import { markdown } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Annotation, EditorState } from "@codemirror/state";
import { tags } from "@lezer/highlight";
import { EditorView } from "@codemirror/view";
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

const externalDocUpdate = Annotation.define<boolean>();

const sourceEditorTheme = EditorView.theme({
  "&": {
    backgroundColor: "var(--background-primary)",
    color: "var(--text-normal)",
    fontFamily: "var(--font-text)",
    fontSize: "var(--font-text-size)",
    fontWeight: "var(--font-text-weight, 400)",
    lineHeight: "1.6",
  },
  ".cm-content": {
    caretColor: "var(--text-normal)",
    minHeight: "60vh",
    padding: "0",
  },
  ".cm-cursor, .cm-dropCursor": {
    borderLeftColor: "var(--text-normal)",
  },
  ".cm-gutters": {
    display: "none",
  },
  ".cm-line": {
    padding: "0",
  },
  ".cm-scroller": {
    fontFamily: "inherit",
    lineHeight: "inherit",
  },
  ".cm-selectionBackground, ::selection": {
    backgroundColor: "var(--text-selection)",
  },
  "&.cm-focused": {
    outline: "none",
  },
});

const sourceEditorHighlighting = syntaxHighlighting(
  HighlightStyle.define([
    { tag: [tags.heading, tags.strong], color: "var(--syntax-keyword)", fontWeight: "700" },
    { tag: tags.emphasis, color: "var(--syntax-function)", fontStyle: "italic" },
    { tag: [tags.link, tags.url], color: "var(--link-color)" },
    { tag: tags.quote, color: "var(--syntax-comment)" },
    { tag: tags.comment, color: "var(--syntax-comment)" },
    { tag: [tags.keyword, tags.modifier, tags.operator], color: "var(--syntax-keyword)" },
    { tag: [tags.string, tags.regexp], color: "var(--syntax-string)" },
    { tag: [tags.number, tags.bool, tags.null], color: "var(--syntax-number)" },
    { tag: [tags.typeName, tags.className], color: "var(--syntax-type)" },
    { tag: [tags.variableName, tags.definition(tags.variableName)], color: "var(--syntax-variable)" },
  ])
);

function MarkdownSourceEditor({
  content,
  setDraft,
}: {
  content: string;
  setDraft: Dispatch<SetStateAction<NoteDraft>>;
}) {
  const editorParentRef = useRef<HTMLDivElement | null>(null);
  const editorViewRef = useRef<EditorView | null>(null);
  const setDraftRef = useRef(setDraft);
  setDraftRef.current = setDraft;

  useEffect(() => {
    const parent = editorParentRef.current;
    if (!parent) return;

    const view = new EditorView({
      parent,
      state: EditorState.create({
        doc: content,
        extensions: [
          markdown({ codeLanguages: languages }),
          sourceEditorHighlighting,
          sourceEditorTheme,
          EditorView.contentAttributes.of({
            "aria-label": "Markdown source editor",
            spellcheck: "true",
          }),
          EditorView.updateListener.of((update) => {
            if (
              !update.docChanged ||
              update.transactions.some((transaction) => transaction.annotation(externalDocUpdate))
            ) {
              return;
            }
            const nextContent = update.state.doc.toString();
            setDraftRef.current((current) =>
              current.content === nextContent ? current : { ...current, content: nextContent }
            );
          }),
        ],
      }),
    });
    editorViewRef.current = view;

    return () => {
      editorViewRef.current = null;
      view.destroy();
    };
  }, []);

  useEffect(() => {
    const view = editorViewRef.current;
    if (!view || view.state.doc.toString() === content) return;
    view.dispatch({
      annotations: externalDocUpdate.of(true),
      changes: { from: 0, insert: content, to: view.state.doc.length },
    });
  }, [content]);

  return <div className="source-editor" ref={editorParentRef} />;
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
  onRestartTerminal,
  overlayContent,
  paneStateKey,
  path,
  resourceKind,
  refreshTick,
  scrollRef,
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
  onRestartTerminal?: (terminalId: string) => void;
  overlayContent?: ReactNode;
  paneStateKey: string;
  path: string | null;
  resourceKind?: "note" | "file";
  refreshTick: number;
  scrollRef?: RefObject<HTMLDivElement | null>;
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
        onRestart={() => onRestartTerminal?.(terminalId)}
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
  focusState,
  notes,
  onOpenNote,
  path,
  refreshTick,
  scrollRef,
}: {
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
              <MarkdownSourceEditor
                content={focusState.draft.content}
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
