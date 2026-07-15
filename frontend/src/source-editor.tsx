import { markdown } from "@codemirror/lang-markdown";
import { languages } from "@codemirror/language-data";
import { HighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { Annotation, EditorState } from "@codemirror/state";
import { tags } from "@lezer/highlight";
import { EditorView } from "@codemirror/view";
import { useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import type { NoteDraft } from "./types";

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

export default function MarkdownSourceEditor({
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
          markdown({ codeLanguages: languages, pasteURLAsLink: false }),
          EditorView.lineWrapping,
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
