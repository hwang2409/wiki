import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  BookOpen,
  ChevronRight,
  ChevronsDownUp,
  FilePlus2,
  Folder as FolderIcon,
  Moon,
  Pencil,
  Search,
  SquarePen,
  Sun,
  X
} from "lucide-react";
import { createNote, getNote, listNotes, searchNotes, updateNote } from "./api";
import {
  ObsidianMarkdown,
  prepareMarkdown,
  splitFrontmatter,
  stripLeadingTitle
} from "./markdown";
import type { Note, NoteDraft, NoteSummary } from "./types";

type Mode = "empty" | "view" | "edit" | "new";
type SidebarTab = "files" | "search";
type Theme = "dark" | "light";

type TreeFolder = {
  name: string;
  path: string;
  folders: TreeFolder[];
  notes: NoteSummary[];
};

const emptyDraft: NoteDraft = { title: "", path: "", content: "" };

function basename(path: string) {
  return path.split("/").pop()?.replace(/\.md$/, "") ?? path;
}

function buildTree(notes: NoteSummary[]): TreeFolder {
  const root: TreeFolder = { name: "", path: "", folders: [], notes: [] };
  const folderIndex = new Map<string, TreeFolder>([["", root]]);

  for (const note of notes) {
    const parts = note.path.split("/");
    let current = root;

    for (const part of parts.slice(0, -1)) {
      const folderPath = current.path ? `${current.path}/${part}` : part;
      let next = folderIndex.get(folderPath);
      if (!next) {
        next = { name: part, path: folderPath, folders: [], notes: [] };
        folderIndex.set(folderPath, next);
        current.folders.push(next);
      }
      current = next;
    }

    current.notes.push(note);
  }

  const sortFolder = (folder: TreeFolder) => {
    folder.folders.sort((a, b) => a.name.localeCompare(b.name));
    folder.notes.sort((a, b) => basename(a.path).localeCompare(basename(b.path)));
    folder.folders.forEach(sortFolder);
  };
  sortFolder(root);

  return root;
}

function collectFolderPaths(folder: TreeFolder, paths: string[] = []): string[] {
  for (const child of folder.folders) {
    paths.push(child.path);
    collectFolderPaths(child, paths);
  }
  return paths;
}

function countWords(content: string) {
  const words = content.trim().split(/\s+/).filter(Boolean).length;
  return { words, characters: content.length };
}

function FolderTree({
  folder,
  depth,
  activePath,
  collapsed,
  onToggleFolder,
  onOpenNote
}: {
  folder: TreeFolder;
  depth: number;
  activePath: string | null;
  collapsed: Set<string>;
  onToggleFolder: (path: string) => void;
  onOpenNote: (path: string) => void;
}) {
  return (
    <>
      {folder.folders.map((child) => {
        const isCollapsed = collapsed.has(child.path);
        return (
          <div className="tree-item" key={child.path}>
            <button
              className="tree-item-self nav-folder-title"
              style={{ paddingInlineStart: `${depth * 17 + 4}px` }}
              type="button"
              onClick={() => onToggleFolder(child.path)}
            >
              <ChevronRight
                className={`collapse-icon${isCollapsed ? " is-collapsed" : ""}`}
                size={16}
              />
              <span className="tree-item-name">{child.name}</span>
            </button>
            {isCollapsed ? null : (
              <FolderTree
                activePath={activePath}
                collapsed={collapsed}
                depth={depth + 1}
                folder={child}
                onOpenNote={onOpenNote}
                onToggleFolder={onToggleFolder}
              />
            )}
          </div>
        );
      })}
      {folder.notes.map((note) => (
        <button
          className={`tree-item-self nav-file-title${
            activePath === note.path ? " is-active" : ""
          }`}
          key={note.id}
          style={{ paddingInlineStart: `${depth * 17 + 24}px` }}
          type="button"
          onClick={() => onOpenNote(note.path)}
        >
          <span className="tree-item-name">{basename(note.path)}</span>
        </button>
      ))}
    </>
  );
}

export default function App() {
  const [notes, setNotes] = useState<NoteSummary[]>([]);
  const [activeNote, setActiveNote] = useState<Note | null>(null);
  const [mode, setMode] = useState<Mode>("empty");
  const [draft, setDraft] = useState<NoteDraft>(emptyDraft);
  const [query, setQuery] = useState("");
  const [sidebarTab, setSidebarTab] = useState<SidebarTab>("files");
  const [collapsedFolders, setCollapsedFolders] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(() =>
    localStorage.getItem("wiki-theme") === "dark" ? "dark" : "light"
  );

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("wiki-theme", theme);
  }, [theme]);

  useEffect(() => {
    let ignore = false;

    async function boot() {
      setIsLoading(true);
      setError(null);
      try {
        const nextNotes = await listNotes();
        if (ignore) return;
        setNotes(nextNotes);
      } catch (err) {
        if (!ignore) setError(err instanceof Error ? err.message : "Could not load notes");
      } finally {
        if (!ignore) setIsLoading(false);
      }
    }

    boot();
    return () => {
      ignore = true;
    };
  }, []);

  const tree = useMemo(() => buildTree(notes), [notes]);

  const [searchResults, setSearchResults] = useState<NoteSummary[]>([]);

  useEffect(() => {
    const needle = query.trim();
    if (!needle) {
      setSearchResults([]);
      return;
    }

    let ignore = false;
    const timer = window.setTimeout(async () => {
      try {
        const results = await searchNotes(needle);
        if (!ignore) setSearchResults(results);
      } catch {
        if (!ignore) setSearchResults([]);
      }
    }, 150);

    return () => {
      ignore = true;
      window.clearTimeout(timer);
    };
  }, [query]);

  async function refreshNotes(selectPath?: string) {
    const nextNotes = await listNotes();
    setNotes(nextNotes);
    const targetPath = selectPath ?? activeNote?.path;
    if (targetPath) setActiveNote(await getNote(targetPath));
  }

  async function openNote(path: string) {
    setError(null);
    setMode("view");
    try {
      const note = await getNote(path);
      setActiveNote(note);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not open note");
    }
  }

  function startNewNote() {
    setError(null);
    setActiveNote(null);
    setDraft(emptyDraft);
    setMode("new");
  }

  function startEditing() {
    if (!activeNote) return;
    setError(null);
    setDraft({
      title: activeNote.title,
      path: activeNote.path,
      content: activeNote.content
    });
    setMode("edit");
  }

  function cancelEditing() {
    setError(null);
    setDraft(emptyDraft);
    setMode(activeNote ? "view" : "empty");
  }

  async function saveDraft() {
    setError(null);
    setIsSaving(true);

    try {
      if (mode === "new") {
        const created = await createNote(draft);
        await refreshNotes(created.path);
        setMode("view");
        return;
      }
      if (activeNote) {
        const updated = await updateNote(activeNote.path, draft.content);
        await refreshNotes(updated.path);
        setMode("view");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save note");
    } finally {
      setIsSaving(false);
    }
  }

  function toggleFolder(path: string) {
    setCollapsedFolders((current) => {
      const next = new Set(current);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  function collapseAll() {
    setCollapsedFolders((current) => {
      const all = collectFolderPaths(tree);
      return current.size === all.length ? new Set() : new Set(all);
    });
  }

  const isEditor = mode === "edit" || mode === "new";
  const canSave = mode === "edit" || draft.title.trim().length > 0;
  const tabTitle =
    mode === "new"
      ? "Untitled"
      : activeNote
        ? basename(activeNote.path)
        : "New tab";
  const breadcrumbs =
    mode === "new"
      ? ["Untitled"]
      : activeNote
        ? activeNote.path.replace(/\.md$/, "").split("/")
        : [];
  const activeParsed = useMemo(
    () => splitFrontmatter(activeNote?.content ?? ""),
    [activeNote?.content]
  );
  const status = countWords(
    isEditor ? draft.content : prepareMarkdown(activeParsed.body)
  );

  return (
    <div className="app-container">
      <div className="workspace-ribbon">
        <button
          aria-label="New note"
          className="ribbon-action"
          title="New note"
          type="button"
          onClick={startNewNote}
        >
          <SquarePen size={18} />
        </button>
        <button
          aria-label="Files"
          className={`ribbon-action${sidebarTab === "files" ? " is-active" : ""}`}
          title="Files"
          type="button"
          onClick={() => setSidebarTab("files")}
        >
          <FolderIcon size={18} />
        </button>
        <button
          aria-label="Search"
          className={`ribbon-action${sidebarTab === "search" ? " is-active" : ""}`}
          title="Search"
          type="button"
          onClick={() => setSidebarTab("search")}
        >
          <Search size={18} />
        </button>
        <div className="ribbon-spacer" />
        <button
          aria-label="Toggle light/dark mode"
          className="ribbon-action"
          title={theme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
          type="button"
          onClick={() => setTheme((current) => (current === "dark" ? "light" : "dark"))}
        >
          {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        </button>
      </div>

      <aside className="workspace-sidebar">
        {sidebarTab === "files" ? (
          <>
            <div className="nav-header">
              <div className="nav-buttons-container">
                <button
                  aria-label="New note"
                  className="nav-action-button"
                  title="New note"
                  type="button"
                  onClick={startNewNote}
                >
                  <FilePlus2 size={16} />
                </button>
                <button
                  aria-label="Collapse all"
                  className="nav-action-button"
                  title="Collapse all"
                  type="button"
                  onClick={collapseAll}
                >
                  <ChevronsDownUp size={16} />
                </button>
              </div>
            </div>
            <div className="nav-files-container">
              {isLoading ? (
                <div className="nav-empty">Loading...</div>
              ) : notes.length > 0 ? (
                <FolderTree
                  activePath={mode === "view" || mode === "edit" ? activeNote?.path ?? null : null}
                  collapsed={collapsedFolders}
                  depth={0}
                  folder={tree}
                  onOpenNote={openNote}
                  onToggleFolder={toggleFolder}
                />
              ) : (
                <div className="nav-empty">No notes yet</div>
              )}
            </div>
          </>
        ) : (
          <div className="search-panel">
            <div className="search-input-container">
              <input
                placeholder="Search..."
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
            <div className="search-results">
              {query.trim() === "" ? (
                <div className="nav-empty">Type to start searching</div>
              ) : searchResults.length > 0 ? (
                searchResults.map((note) => (
                  <button
                    className="search-result"
                    key={note.id}
                    type="button"
                    onClick={() => openNote(note.path)}
                  >
                    <span className="search-result-title">{basename(note.path)}</span>
                    <span className="search-result-path">{note.path}</span>
                    {note.excerpt ? (
                      <span className="search-result-excerpt">{note.excerpt}</span>
                    ) : null}
                  </button>
                ))
              ) : (
                <div className="nav-empty">No matches found</div>
              )}
            </div>
          </div>
        )}
      </aside>

      <main className="workspace-leaf">
        <div className="workspace-tab-header">
          <div className="workspace-tab">
            <span>{tabTitle}</span>
          </div>
        </div>

        <div className="view-header">
          <div className="view-header-title-container">
            {breadcrumbs.map((crumb, index) => (
              <span className="view-header-breadcrumb" key={`${crumb}-${index}`}>
                {index > 0 ? <ChevronRight size={14} /> : null}
                <span>{crumb}</span>
              </span>
            ))}
          </div>
          <div className="view-actions">
            {mode === "view" ? (
              <button
                aria-label="Edit this note"
                className="view-action"
                title="Edit this note"
                type="button"
                onClick={startEditing}
              >
                <Pencil size={16} />
              </button>
            ) : null}
            {isEditor ? (
              <>
                <button
                  aria-label="Save and read"
                  className="view-action"
                  disabled={!canSave || isSaving}
                  title="Save and switch to reading view"
                  type="button"
                  onClick={saveDraft}
                >
                  <BookOpen size={16} />
                </button>
                <button
                  aria-label="Discard changes"
                  className="view-action"
                  title="Discard changes"
                  type="button"
                  onClick={cancelEditing}
                >
                  <X size={16} />
                </button>
              </>
            ) : null}
          </div>
        </div>

        {error ? (
          <div className="notice" role="alert">
            <AlertCircle size={16} />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="view-content">
          {mode === "empty" ? (
            <div className="empty-state">
              <div className="empty-state-title">No file is open</div>
              <div className="empty-state-actions">
                <button type="button" onClick={startNewNote}>
                  Create new note
                </button>
                {notes.length > 0 ? (
                  <button type="button" onClick={() => openNote(notes[0].path)}>
                    Open most recent note
                  </button>
                ) : null}
              </div>
            </div>
          ) : mode === "view" && activeNote ? (
            <div className="markdown-reading-view" key={activeNote.path}>
              <div className="markdown-sizer">
                <h1 className="inline-title">{activeNote.title}</h1>
                {activeParsed.properties && activeParsed.properties.length > 0 ? (
                  <div className="metadata-container" aria-label="Properties">
                    {activeParsed.properties.map(([key, value]) => (
                      <div className="metadata-property" key={key}>
                        <span className="metadata-property-key">{key}</span>
                        <span className="metadata-property-value">
                          {Array.isArray(value) ? (
                            value.map((item) => (
                              <span className="metadata-pill" key={item}>
                                {item}
                              </span>
                            ))
                          ) : (
                            value
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                ) : null}
                <div className="markdown-preview-view">
                  <ObsidianMarkdown
                    content={stripLeadingTitle(activeParsed.body, activeNote.title)}
                    notes={notes}
                    onOpenNote={openNote}
                  />
                </div>
              </div>
            </div>
          ) : isEditor ? (
            <div className="markdown-source-view">
              <div className="markdown-sizer">
                {mode === "new" ? (
                  <div className="new-note-meta">
                    <input
                      className="inline-title-input"
                      placeholder="Untitled"
                      type="text"
                      value={draft.title}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, title: event.target.value }))
                      }
                    />
                    <input
                      className="note-path-input"
                      placeholder="folder/note.md (optional)"
                      type="text"
                      value={draft.path}
                      onChange={(event) =>
                        setDraft((current) => ({ ...current, path: event.target.value }))
                      }
                    />
                  </div>
                ) : (
                  <h1 className="inline-title">{draft.title}</h1>
                )}
                <textarea
                  className="source-editor"
                  spellCheck="true"
                  value={draft.content}
                  onChange={(event) =>
                    setDraft((current) => ({ ...current, content: event.target.value }))
                  }
                />
              </div>
            </div>
          ) : null}
        </div>

        {mode !== "empty" ? (
          <div className="status-bar">
            <span>{status.words} words</span>
            <span>{status.characters} characters</span>
          </div>
        ) : null}
      </main>
    </div>
  );
}
