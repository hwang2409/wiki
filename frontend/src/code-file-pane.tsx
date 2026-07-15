import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";
import { Search, X } from "lucide-react";
import { getFileContent, type FileContent } from "./api";
import { languageForPath, highlightToHtml, useCurrentTheme } from "./shiki";

function HighlightedLine({ line, needle }: { line: string; needle: string }) {
  if (!needle) return <>{line || " "}</>;
  const lower = line.toLocaleLowerCase();
  const wanted = needle.toLocaleLowerCase();
  const parts: ReactNode[] = [];
  let cursor = 0;
  let match = lower.indexOf(wanted);
  while (match >= 0) {
    parts.push(line.slice(cursor, match));
    parts.push(<mark key={match}>{line.slice(match, match + needle.length)}</mark>);
    cursor = match + needle.length;
    match = lower.indexOf(wanted, cursor);
  }
  parts.push(line.slice(cursor));
  return <>{parts}</>;
}

function matchCount(content: string, needle: string) {
  if (!needle) return 0;
  return content.toLocaleLowerCase().split(needle.toLocaleLowerCase()).length - 1;
}

function decorateMatches(html: string | null, needle: string) {
  if (!html || !needle || typeof document === "undefined") return html;
  const root = document.createElement("div");
  root.innerHTML = html;
  const wanted = needle.toLocaleLowerCase();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes: Text[] = [];
  let node = walker.nextNode();
  while (node) {
    if (!node.parentElement?.closest("mark")) textNodes.push(node as Text);
    node = walker.nextNode();
  }
  for (const textNode of textNodes) {
    const value = textNode.nodeValue ?? "";
    const lower = value.toLocaleLowerCase();
    let cursor = 0;
    let match = lower.indexOf(wanted);
    if (match < 0) continue;
    const fragment = document.createDocumentFragment();
    while (match >= 0) {
      fragment.append(value.slice(cursor, match));
      const mark = document.createElement("mark");
      mark.textContent = value.slice(match, match + needle.length);
      fragment.append(mark);
      cursor = match + needle.length;
      match = lower.indexOf(wanted, cursor);
    }
    fragment.append(value.slice(cursor));
    textNode.replaceWith(fragment);
  }
  return root.innerHTML;
}

export function CodeFilePane({ path, scrollRef }: { path: string; scrollRef?: RefObject<HTMLDivElement | null> }) {
  const theme = useCurrentTheme();
  const findInput = useRef<HTMLInputElement | null>(null);
  const [file, setFile] = useState<FileContent | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [findOpen, setFindOpen] = useState(false);
  const [find, setFind] = useState("");
  const [highlighted, setHighlighted] = useState<string | null>(null);

  useEffect(() => {
    let ignore = false;
    setFile(null);
    setError(null);
    setHighlighted(null);
    getFileContent(path)
      .then((result) => {
        if (!ignore) setFile(result);
      })
      .catch((err) => {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not open file");
      });
    return () => {
      ignore = true;
    };
  }, [path]);

  useEffect(() => {
    if (!file || file.binary || file.content === null) return;
    const language = languageForPath(path);
    if (!language) {
      setHighlighted(null);
      return;
    }
    let ignore = false;
    highlightToHtml(file.content, language, theme).then((html) => {
      if (!ignore) setHighlighted(html);
    });
    return () => {
      ignore = true;
    };
  }, [file, path, theme]);

  const content = file?.content ?? "";
  const lines = content.split("\n");
  const matches = matchCount(content, find);
  const renderedHighlighted = useMemo(
    () => decorateMatches(highlighted, find),
    [find, highlighted]
  );

  function openFind() {
    setFindOpen(true);
    requestAnimationFrame(() => findInput.current?.focus());
  }

  return (
    <section
      className="secondary-pane code-file-pane"
      aria-label={`Code file: ${path}`}
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === "f") {
          event.preventDefault();
          openFind();
        }
      }}
    >
      <div className="code-file-toolbar">
        <span className="code-file-language">{languageForPath(path) ?? "text"}</span>
        <button data-code-file-find="true" type="button" onClick={openFind}>
          <Search size={12} /> Find
        </button>
        {findOpen ? (
          <label className="artifact-code-find">
            <Search size={12} />
            <input
              ref={findInput}
              aria-label="Find in file"
              value={find}
              onChange={(event) => setFind(event.target.value)}
            />
            <span>{matches} matches</span>
            <button
              aria-label="Close find"
              type="button"
              onClick={() => {
                setFindOpen(false);
                setFind("");
              }}
            >
              <X size={11} />
            </button>
          </label>
        ) : null}
      </div>
      <div className="code-file-path">{path}</div>
      <div className="code-file-scroll" ref={scrollRef}>
        {error ? (
          <div className="notice" role="alert">{error}</div>
        ) : !file ? (
          <div className="nav-empty">Loading file...</div>
        ) : file.binary ? (
          <div className="code-file-placeholder">
            <strong>Binary file</strong>
            <span>This file cannot be displayed as text.</span>
          </div>
        ) : renderedHighlighted ? (
          <div className="code-file-highlighted" dangerouslySetInnerHTML={{ __html: renderedHighlighted }} />
        ) : (
          <pre className="code-file-source">
            {lines.map((line, index) => (
              <span className="code-file-line" key={index}>
                <span className="code-file-gutter">{index + 1}</span>
                <code><HighlightedLine line={line} needle={find} /></code>
              </span>
            ))}
          </pre>
        )}
      </div>
    </section>
  );
}
