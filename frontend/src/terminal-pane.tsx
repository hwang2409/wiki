import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { Info } from "lucide-react";
import "@xterm/xterm/css/xterm.css";
import {
  getTerminalRuntime,
  type TerminalRenderer,
  type TerminalSearchResults,
  type TerminalStatus,
} from "./terminal-runtime";

export type TerminalPaneController = {
  focus: () => void;
  sendInput: (data: string) => void;
};

type RuntimeSnapshot = {
  customName: string | null;
  cwd: string | null;
  message: string;
  oscTitle: string | null;
  renderer: TerminalRenderer;
  shell: string | null;
  size: { cols: number; rows: number } | null;
  status: TerminalStatus;
  theme: {
    chromeVars: Record<string, string>;
  };
};

const STATUS_LABELS: Record<TerminalStatus, string> = {
  connecting: "connecting…",
  live: "live",
  ended: "ended",
  error: "error",
};

function shellBasename(shell: string | null) {
  if (!shell) return null;
  const base = shell.split("/").pop();
  return base ? base : null;
}

export function terminalDisplayTitle(snapshot: {
  customName: string | null;
  cwd: string | null;
  oscTitle: string | null;
  shell: string | null;
}) {
  if (snapshot.customName) return snapshot.customName;
  if (snapshot.oscTitle) return snapshot.oscTitle;
  const shell = shellBasename(snapshot.shell);
  if (shell && snapshot.cwd) return `${shell} — ${snapshot.cwd}`;
  if (shell) return shell;
  return "terminal";
}

export function TerminalPane({
  customName,
  focused,
  launchNonce,
  onNameChange,
  onRegisterController,
  onRestart,
  terminalId,
}: {
  customName?: string | null;
  focused: boolean;
  launchNonce: number;
  onNameChange?: (terminalId: string, name: string | null) => void;
  onRegisterController?: (terminalId: string, controller: TerminalPaneController | null) => void;
  onRestart: () => void;
  terminalId: string;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const runtime = useMemo(() => getTerminalRuntime(terminalId), [terminalId]);
  const [snapshot, setSnapshot] = useState<RuntimeSnapshot>(() => runtime.getSnapshot());
  const [findOpen, setFindOpen] = useState(false);
  const [findTerm, setFindTerm] = useState("");
  const [searchResults, setSearchResults] = useState<TerminalSearchResults>({
    resultIndex: -1,
    resultCount: 0,
  });
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const [sizeFlash, setSizeFlash] = useState<{ cols: number; rows: number } | null>(null);
  const findInputRef = useRef<HTMLInputElement | null>(null);
  const renameInputRef = useRef<HTMLInputElement | null>(null);
  const detailsRef = useRef<HTMLDivElement | null>(null);
  const sizeFlashTimer = useRef<number | null>(null);
  const seenFirstSize = useRef(false);

  useEffect(() => {
    setSnapshot(runtime.getSnapshot());
    const unsubscribe = runtime.subscribe(() => {
      setSnapshot(runtime.getSnapshot());
    });
    return () => {
      unsubscribe();
    };
  }, [runtime]);

  useEffect(() => {
    const unsubscribe = runtime.onSearchResults((results) => setSearchResults(results));
    return () => {
      unsubscribe();
    };
  }, [runtime]);

  useEffect(() => {
    if (findOpen) {
      findInputRef.current?.focus();
      findInputRef.current?.select();
    }
  }, [findOpen]);

  useEffect(() => {
    if (renaming) {
      renameInputRef.current?.focus();
      renameInputRef.current?.select();
    }
  }, [renaming]);

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    runtime.attach(host);
    return () => runtime.detach();
  }, [runtime]);

  useEffect(() => {
    runtime.ensureConnection(launchNonce);
  }, [launchNonce, runtime]);

  useEffect(() => {
    if (focused) runtime.focus();
  }, [focused, runtime]);

  useEffect(() => {
    const controller: TerminalPaneController = {
      focus() {
        runtime.focus();
      },
      sendInput(data: string) {
        runtime.sendInput(data);
      },
    };
    onRegisterController?.(terminalId, controller);
    return () => onRegisterController?.(terminalId, null);
  }, [onRegisterController, runtime, terminalId]);

  useEffect(() => {
    if (!detailsOpen) return;
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && detailsRef.current?.contains(event.target)) return;
      setDetailsOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [detailsOpen]);

  const snapshotSize = snapshot.size;
  useEffect(() => {
    if (!snapshotSize) return;
    if (!seenFirstSize.current) {
      seenFirstSize.current = true;
      return;
    }
    setSizeFlash(snapshotSize);
    if (sizeFlashTimer.current !== null) window.clearTimeout(sizeFlashTimer.current);
    sizeFlashTimer.current = window.setTimeout(() => {
      sizeFlashTimer.current = null;
      setSizeFlash(null);
    }, 900);
  }, [snapshotSize]);

  useEffect(
    () => () => {
      if (sizeFlashTimer.current !== null) window.clearTimeout(sizeFlashTimer.current);
    },
    []
  );

  function closeFind() {
    setFindOpen(false);
    setFindTerm("");
    runtime.clearSearch();
    window.requestAnimationFrame(() => runtime.focus());
  }

  function openFind() {
    setFindOpen(true);
  }

  function beginRename() {
    setDetailsOpen(false);
    setRenameDraft(customName ?? snapshot.customName ?? "");
    setRenaming(true);
  }

  function commitRename() {
    const nextName = renameDraft.trim() || null;
    runtime.setCustomName(renameDraft);
    onNameChange?.(terminalId, nextName);
    setRenaming(false);
    window.requestAnimationFrame(() => runtime.focus());
  }

  function cancelRename() {
    setRenaming(false);
    window.requestAnimationFrame(() => runtime.focus());
  }

  function handlePaneKeyDownCapture(event: ReactKeyboardEvent<HTMLElement>) {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      event.stopPropagation();
      openFind();
    }
  }

  function handleFindContainerKeyDown(event: ReactKeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeFind();
    }
  }

  function handleFindInputKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      runtime.search(findTerm, event.shiftKey ? "previous" : "next");
    }
  }

  function handleRenameKeyDown(event: ReactKeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") {
      event.preventDefault();
      event.stopPropagation();
      commitRename();
    } else if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      cancelRename();
    }
  }

  function updateFindTerm(term: string) {
    setFindTerm(term);
    runtime.search(term);
  }

  const resultLabel =
    searchResults.resultCount === 0
      ? findTerm
        ? "No matches"
        : ""
      : `${searchResults.resultIndex + 1} of ${searchResults.resultCount}`;

  const chromeStyle = useMemo(
    () => snapshot.theme.chromeVars as CSSProperties,
    [snapshot.theme.chromeVars]
  );

  const title = terminalDisplayTitle({ ...snapshot, customName: customName ?? snapshot.customName });
  const statusLabel = STATUS_LABELS[snapshot.status];

  return (
    <section
      className="secondary-pane terminal-pane"
      aria-label={`Terminal: ${terminalId}`}
      data-terminal-pane="true"
      data-terminal-status={snapshot.status}
      onKeyDownCapture={handlePaneKeyDownCapture}
    >
      <div className="secondary-pane-header terminal-pane-header" style={chromeStyle}>
        <div className="secondary-pane-title terminal-pane-title-group">
          <span
            className={`terminal-pane-status-dot is-${snapshot.status}`}
            title={statusLabel}
            aria-hidden="true"
          />
          {renaming ? (
            <input
              ref={renameInputRef}
              className="terminal-pane-rename-input"
              aria-label="Terminal name"
              autoComplete="off"
              spellCheck={false}
              placeholder={title}
              value={renameDraft}
              onBlur={commitRename}
              onChange={(event) => setRenameDraft(event.target.value)}
              onKeyDown={handleRenameKeyDown}
            />
          ) : (
            <button
              className="terminal-pane-title"
              type="button"
              title={`terminal://${terminalId} — double-click to rename`}
              onDoubleClick={beginRename}
            >
              {title}
            </button>
          )}
        </div>
        <div className="terminal-pane-meta">
          {findOpen ? (
            <div
              className="terminal-pane-find"
              role="search"
              onKeyDownCapture={handleFindContainerKeyDown}
            >
              <input
                ref={findInputRef}
                aria-label="Find in terminal"
                autoComplete="off"
                spellCheck={false}
                placeholder="Find"
                value={findTerm}
                onChange={(event) => updateFindTerm(event.target.value)}
                onKeyDown={handleFindInputKeyDown}
              />
              <span className="terminal-pane-find-count" aria-live="polite">
                {resultLabel}
              </span>
              <button
                className="terminal-pane-find-button"
                type="button"
                aria-label="Previous match"
                disabled={!findTerm}
                onClick={() => runtime.search(findTerm, "previous")}
              >
                ↑
              </button>
              <button
                className="terminal-pane-find-button"
                type="button"
                aria-label="Next match"
                disabled={!findTerm}
                onClick={() => runtime.search(findTerm, "next")}
              >
                ↓
              </button>
              <button
                className="terminal-pane-find-button"
                type="button"
                aria-label="Close find"
                onClick={closeFind}
              >
                Esc
              </button>
            </div>
          ) : null}
          {snapshot.status !== "live" ? (
            <span className={`terminal-pane-status-word is-${snapshot.status}`}>{statusLabel}</span>
          ) : null}
          <div className="terminal-pane-details-anchor" ref={detailsRef}>
            <button
              className={`view-action terminal-pane-details-toggle${detailsOpen ? " is-open" : ""}`}
              type="button"
              aria-label="Terminal details"
              aria-expanded={detailsOpen}
              onClick={() => setDetailsOpen((open) => !open)}
            >
              <Info size={13} aria-hidden="true" />
            </button>
            {detailsOpen ? (
              <div className="terminal-pane-details" role="group" aria-label="Terminal details">
                <div className="terminal-pane-details-row">
                  <span className="terminal-pane-details-label">session</span>
                  <span className="terminal-pane-details-value">terminal://{terminalId}</span>
                </div>
                <div className="terminal-pane-details-row">
                  <span className="terminal-pane-details-label">status</span>
                  <span className="terminal-pane-details-value">{statusLabel}</span>
                </div>
                {snapshot.shell ? (
                  <div className="terminal-pane-details-row">
                    <span className="terminal-pane-details-label">shell</span>
                    <span className="terminal-pane-details-value">{snapshot.shell}</span>
                  </div>
                ) : null}
                {snapshot.cwd ? (
                  <div className="terminal-pane-details-row">
                    <span className="terminal-pane-details-label">cwd</span>
                    <span className="terminal-pane-details-value">{snapshot.cwd}</span>
                  </div>
                ) : null}
                <div className="terminal-pane-details-row">
                  <span className="terminal-pane-details-label">renderer</span>
                  <span className="terminal-pane-details-value">{snapshot.renderer}</span>
                </div>
                {snapshot.size ? (
                  <div className="terminal-pane-details-row">
                    <span className="terminal-pane-details-label">size</span>
                    <span className="terminal-pane-details-value">
                      {snapshot.size.cols}×{snapshot.size.rows}
                    </span>
                  </div>
                ) : null}
                <div className="terminal-pane-details-actions">
                  <button className="view-action" type="button" onClick={beginRename}>
                    rename
                  </button>
                  <button
                    className="view-action"
                    type="button"
                    onClick={() => {
                      setDetailsOpen(false);
                      onRestart();
                    }}
                  >
                    restart
                  </button>
                </div>
              </div>
            ) : null}
          </div>
        </div>
      </div>
      <div className="secondary-pane-content terminal-pane-content" style={chromeStyle}>
        <div className="terminal-pane-host" ref={hostRef} />
        {sizeFlash ? (
          <div className="terminal-pane-size-flash" aria-hidden="true">
            {sizeFlash.cols}×{sizeFlash.rows}
          </div>
        ) : null}
        {snapshot.status === "live" ? null : (
          <div className="terminal-pane-overlay">
            <div className="terminal-pane-notice">{snapshot.message}</div>
            <button className="terminal-pane-restart" type="button" onClick={onRestart}>
              Restart shell
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
