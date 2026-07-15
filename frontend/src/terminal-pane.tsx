import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import "@xterm/xterm/css/xterm.css";
import {
  getTerminalRuntime,
  type TerminalRenderer,
  type TerminalSearchResults,
  type TerminalStatus,
} from "./terminal-runtime";

export type TerminalPaneController = {
  sendInput: (data: string) => void;
};

type RuntimeSnapshot = {
  message: string;
  renderer: TerminalRenderer;
  status: TerminalStatus;
  theme: {
    chromeVars: Record<string, string>;
  };
};

export function TerminalPane({
  focused,
  launchNonce,
  onRegisterController,
  onRestart,
  terminalId,
}: {
  focused: boolean;
  launchNonce: number;
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
  const findInputRef = useRef<HTMLInputElement | null>(null);

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
      sendInput(data: string) {
        runtime.sendInput(data);
      },
    };
    onRegisterController?.(terminalId, controller);
    return () => onRegisterController?.(terminalId, null);
  }, [onRegisterController, runtime, terminalId]);

  function closeFind() {
    setFindOpen(false);
    setFindTerm("");
    runtime.clearSearch();
    window.requestAnimationFrame(() => runtime.focus());
  }

  function openFind() {
    setFindOpen(true);
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

  return (
    <section
      className="secondary-pane terminal-pane"
      aria-label={`Terminal: ${terminalId}`}
      data-terminal-pane="true"
      onKeyDownCapture={handlePaneKeyDownCapture}
    >
      <div className="secondary-pane-header terminal-pane-header" style={chromeStyle}>
        <div className="secondary-pane-title">
          <span className="terminal-pane-title">terminal://{terminalId}</span>
        </div>
        <div className="terminal-pane-meta">
          <span className="terminal-pane-chip">{snapshot.renderer}</span>
          <span className={`terminal-pane-chip is-${snapshot.status}`}>{snapshot.status}</span>
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
          <button className="view-action terminal-pane-action" type="button" onClick={onRestart}>
            restart
          </button>
        </div>
      </div>
      <div className="secondary-pane-content terminal-pane-content" style={chromeStyle}>
        <div className="terminal-pane-host" ref={hostRef} />
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
