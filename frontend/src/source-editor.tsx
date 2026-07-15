import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Annotation, EditorState, Transaction } from "@codemirror/state";
import { tags } from "@lezer/highlight";
import { EditorView, keymap } from "@codemirror/view";
import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import type { NoteDraft } from "./types";

const externalDocUpdate = Annotation.define<boolean>();

export type SourceEditorHandoff = {
  anchor: number;
  content: string;
  focused: boolean;
  head: number;
  notePath: string;
  selectionDirection: "backward" | "forward" | "none";
};

type LiveEditor = {
  active: boolean;
  notePath: string;
  view: EditorView | null;
};

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

export default function MarkdownSourceEditor({
  content,
  handoffRef,
  notePath,
  setDraft,
}: {
  content: string;
  handoffRef: { current: SourceEditorHandoff | null };
  notePath: string;
  setDraft: Dispatch<SetStateAction<NoteDraft>>;
}) {
  const editorParentRef = useRef<HTMLDivElement | null>(null);
  const editorViewRef = useRef<EditorView | null>(null);
  const liveEditorRef = useRef<LiveEditor | null>(null);
  const mountedRef = useRef(false);
  const currentContentRef = useRef(content);
  const currentNotePathRef = useRef(notePath);
  currentContentRef.current = content;
  currentNotePathRef.current = notePath;
  const setDraftRef = useRef(setDraft);
  setDraftRef.current = setDraft;

  useEffect(() => {
    const parent = editorParentRef.current;
    if (!parent) return;
    mountedRef.current = true;

    let view: EditorView | null = null;
    const liveEditor: LiveEditor = {
      active: true,
      notePath,
      view: null as EditorView | null,
    };
    const isLiveView = () =>
      mountedRef.current &&
      liveEditor.active &&
      liveEditor.view === view &&
      view !== null &&
      editorViewRef.current === view &&
      liveEditorRef.current === liveEditor;
    const isLiveCurrentNote = () => isLiveView() && liveEditor.notePath === currentNotePathRef.current;
    const handoff = handoffRef.current;
    const canRestoreHandoff = handoff?.focused === true && handoff.notePath === notePath;
    const initialContent = canRestoreHandoff ? handoff.content : content;

    view = new EditorView({
      parent,
      state: EditorState.create({
        doc: initialContent,
        extensions: [
          markdown({ codeLanguages: languages, pasteURLAsLink: false }),
          EditorView.lineWrapping,
          history(),
          keymap.of([...defaultKeymap, ...historyKeymap]),
          sourceEditorHighlighting,
          sourceEditorTheme,
          EditorView.contentAttributes.of({
            "aria-label": "Markdown source editor",
            spellcheck: "true",
          }),
          EditorView.updateListener.of((update) => {
            if (
              !update.docChanged ||
              !isLiveCurrentNote() ||
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
    liveEditor.view = view;
    editorViewRef.current = view;
    liveEditorRef.current = liveEditor;

    if (isLiveCurrentNote() && canRestoreHandoff && initialContent !== content) {
      view.dispatch({
        annotations: [
          Transaction.addToHistory.of(true),
          Transaction.userEvent.of("input"),
        ],
        changes: { from: 0, insert: content, to: view.state.doc.length },
      });
    }

    if (isLiveView() && canRestoreHandoff) {
      const clamp = (offset: number) => Math.max(0, Math.min(offset, view.state.doc.length));
      const start = clamp(handoff.anchor);
      const end = clamp(handoff.head);
      const selection =
        handoff.selectionDirection === "backward"
          ? { anchor: end, head: start }
          : { anchor: start, head: end };
      if (isLiveView()) view.dispatch({ selection });
      if (isLiveView()) view.focus();
    }
    handoffRef.current = null;

    return () => {
      mountedRef.current = false;
      liveEditor.active = false;
      if (liveEditorRef.current === liveEditor) liveEditorRef.current = null;
      if (editorViewRef.current === view) editorViewRef.current = null;
      view.destroy();
    };
  }, []);

  useEffect(() => {
    const view = editorViewRef.current;
    const liveEditor = liveEditorRef.current;
    if (
      !view ||
      !liveEditor ||
      !mountedRef.current ||
      !liveEditor.active ||
      liveEditor.view !== view ||
      currentNotePathRef.current !== notePath ||
      currentContentRef.current !== content
    ) {
      return;
    }
    if (liveEditor.notePath !== notePath || view.state.doc.toString() !== content) {
      view.dispatch({
        annotations: [
          externalDocUpdate.of(true),
          Transaction.addToHistory.of(false),
          Transaction.remote.of(true),
        ],
        changes: { from: 0, insert: content, to: view.state.doc.length },
      });
      if (!mountedRef.current || !liveEditor.active || liveEditorRef.current !== liveEditor) return;
      liveEditor.notePath = notePath;
    }
  }, [content, notePath]);

  return <div className="source-editor" ref={editorParentRef} />;
}
