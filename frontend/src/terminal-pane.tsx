import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import "@xterm/xterm/css/xterm.css";
import {
  getTerminalRuntime,
  type TerminalRenderer,
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

  const chromeStyle = useMemo(
    () => snapshot.theme.chromeVars as CSSProperties,
    [snapshot.theme.chromeVars]
  );

  return (
    <section className="secondary-pane terminal-pane" aria-label={`Terminal: ${terminalId}`}>
      <div className="secondary-pane-header terminal-pane-header" style={chromeStyle}>
        <div className="secondary-pane-title">
          <span className="terminal-pane-title">terminal://{terminalId}</span>
        </div>
        <div className="terminal-pane-meta">
          <span className="terminal-pane-chip">{snapshot.renderer}</span>
          <span className={`terminal-pane-chip is-${snapshot.status}`}>{snapshot.status}</span>
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
