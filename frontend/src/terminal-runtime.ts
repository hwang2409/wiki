import { FitAddon } from "@xterm/addon-fit";
import { WebglAddon } from "@xterm/addon-webgl";
import { Terminal } from "@xterm/xterm";
import type { IDisposable } from "@xterm/xterm";
import { deriveTerminalTheme, type DerivedTerminalTheme } from "./terminal-theme";

export type TerminalStatus = "connecting" | "live" | "ended" | "error";
export type TerminalRenderer = "webgl" | "dom";

type TerminalSnapshot = {
  message: string;
  renderer: TerminalRenderer;
  status: TerminalStatus;
  theme: DerivedTerminalTheme;
};

declare global {
  interface Window {
    __wikiTerminals?: Record<
      string,
      {
        renderer: () => TerminalRenderer;
        socketReadyState: () => number;
        status: () => TerminalStatus;
        terminal: Terminal;
      }
    >;
  }
}

const DEFAULT_COLS = 80;
const DEFAULT_ROWS = 24;
const RESIZE_DEBOUNCE_MS = 48;

function terminalWsUrl(terminalId: string, token: string, create: boolean) {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const params = new URLSearchParams({ token });
  if (create) params.set("create", "1");
  return `${protocol}//${window.location.host}/ws/terminal/${encodeURIComponent(terminalId)}?${params.toString()}`;
}

async function getTerminalToken() {
  const response = await fetch("/api/terminal-token");
  if (!response.ok) throw new Error(`terminal token failed: ${response.status}`);
  return response.json() as Promise<{ token: string }>;
}

function readTerminalFontFamily() {
  return getComputedStyle(document.documentElement).getPropertyValue("--font-monospace").trim();
}

function readTerminalFontSize() {
  const raw = getComputedStyle(document.documentElement).getPropertyValue("--font-ui-small").trim();
  const parsed = Number.parseFloat(raw);
  return Number.isFinite(parsed) ? parsed + 0.5 : 14;
}

let parkingLot: HTMLDivElement | null = null;

function getParkingLot() {
  if (parkingLot && parkingLot.isConnected) return parkingLot;
  const root = document.createElement("div");
  root.id = "wiki-terminal-parking-lot";
  Object.assign(root.style, {
    height: "1px",
    left: "-10000px",
    overflow: "hidden",
    pointerEvents: "none",
    position: "fixed",
    top: "-10000px",
    width: "1px",
  });
  document.body.appendChild(root);
  parkingLot = root;
  return root;
}

class TerminalRuntime {
  private readonly terminalId: string;
  private readonly terminal: Terminal;
  private readonly fitAddon: FitAddon;
  private readonly shellRoot: HTMLDivElement;
  private readonly listeners = new Set<() => void>();
  private readonly themeObserver: MutationObserver;
  private readonly dataDisposable: IDisposable;
  private rendererDisposable: IDisposable | null = null;
  private webglAddon: WebglAddon | null = null;
  private socket: WebSocket | null = null;
  private resizeObserver: ResizeObserver | null = null;
  private attachedHost: HTMLDivElement | null = null;
  private resizeTimer: number | null = null;
  private connectGeneration = 0;
  private lastLaunchNonce = 0;
  private lastSize: { cols: number; rows: number } | null = null;
  private hasConnected = false;
  private disposed = false;
  private snapshot: TerminalSnapshot = {
    message: "Connecting…",
    renderer: "dom",
    status: "connecting",
    theme: deriveTerminalTheme(),
  };

  constructor(terminalId: string) {
    this.terminalId = terminalId;
    this.shellRoot = document.createElement("div");
    this.shellRoot.className = "terminal-runtime-shell";
    this.shellRoot.style.height = "1px";
    this.shellRoot.style.width = "1px";

    this.terminal = new Terminal({
      allowTransparency: false,
      convertEol: false,
      cursorBlink: false,
      cursorInactiveStyle: "outline",
      cursorStyle: "block",
      customGlyphs: true,
      fontFamily: readTerminalFontFamily(),
      fontSize: readTerminalFontSize(),
      lineHeight: 1.18,
      scrollback: 5000,
      theme: this.snapshot.theme.renderer,
    });
    this.fitAddon = new FitAddon();
    this.terminal.loadAddon(this.fitAddon);
    this.terminal.open(this.shellRoot);
    this.terminal.textarea?.setAttribute("data-terminal-input", "true");
    this.terminal.textarea?.setAttribute("data-terminal-id", terminalId);
    this.installRenderer();
    this.dataDisposable = this.terminal.onData((data) => {
      this.sendInput(data);
    });
    this.themeObserver = new MutationObserver(() => {
      this.applyTheme();
    });
    this.themeObserver.observe(document.documentElement, {
      attributeFilter: ["data-theme", "style"],
      attributes: true,
    });
    document.fonts?.ready.then(() => this.scheduleResize()).catch(() => {});
    getParkingLot().appendChild(this.shellRoot);
    window.__wikiTerminals = window.__wikiTerminals ?? {};
    window.__wikiTerminals[terminalId] = {
      renderer: () => this.snapshot.renderer,
      socketReadyState: () => this.socket?.readyState ?? -1,
      status: () => this.snapshot.status,
      terminal: this.terminal,
    };
  }

  getSnapshot() {
    return this.snapshot;
  }

  subscribe(listener: () => void) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  attach(host: HTMLDivElement) {
    if (this.disposed) return;
    if (this.attachedHost === host && host.contains(this.shellRoot)) {
      this.scheduleResize();
      return;
    }
    this.disconnectResizeObserver();
    this.attachedHost = host;
    this.shellRoot.style.height = "100%";
    this.shellRoot.style.width = "100%";
    host.appendChild(this.shellRoot);
    this.resizeObserver = new ResizeObserver(() => {
      this.scheduleResize();
    });
    this.resizeObserver.observe(host);
    this.scheduleResize();
  }

  detach() {
    if (this.disposed) return;
    this.disconnectResizeObserver();
    this.attachedHost = null;
    this.shellRoot.style.height = "1px";
    this.shellRoot.style.width = "1px";
    getParkingLot().appendChild(this.shellRoot);
  }

  focus() {
    this.terminal.focus();
  }

  ensureConnection(launchNonce: number) {
    if (this.disposed) return;
    if (!this.hasConnected) {
      this.hasConnected = true;
      this.lastLaunchNonce = launchNonce;
      void this.connect(launchNonce > 0);
      return;
    }
    if (launchNonce > this.lastLaunchNonce) {
      this.lastLaunchNonce = launchNonce;
      void this.connect(true);
    }
  }

  sendInput(data: string) {
    if (!data) return;
    const socket = this.socket;
    if (socket?.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({ type: "input", data }));
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.listeners.clear();
    this.disconnectResizeObserver();
    if (this.resizeTimer !== null) {
      window.clearTimeout(this.resizeTimer);
      this.resizeTimer = null;
    }
    this.themeObserver.disconnect();
    this.rendererDisposable?.dispose();
    this.rendererDisposable = null;
    this.webglAddon?.dispose();
    this.webglAddon = null;
    this.connectGeneration += 1;
    const socket = this.socket;
    this.socket = null;
    socket?.close();
    this.dataDisposable.dispose();
    delete window.__wikiTerminals?.[this.terminalId];
    this.terminal.dispose();
    this.shellRoot.remove();
  }

  private emit() {
    this.listeners.forEach((listener) => listener());
  }

  private setSnapshot(next: Partial<TerminalSnapshot>) {
    this.snapshot = { ...this.snapshot, ...next };
    this.emit();
  }

  private installRenderer() {
    try {
      this.webglAddon = new WebglAddon();
      this.terminal.loadAddon(this.webglAddon);
      this.setSnapshot({ renderer: "webgl" });
      this.shellRoot.dataset.terminalRenderer = "webgl";
      this.rendererDisposable = this.webglAddon.onContextLoss(() => {
        this.rendererDisposable?.dispose();
        this.rendererDisposable = null;
        this.webglAddon?.dispose();
        this.webglAddon = null;
        this.setSnapshot({ renderer: "dom" });
        this.shellRoot.dataset.terminalRenderer = "dom";
      });
    } catch {
      this.setSnapshot({ renderer: "dom" });
      this.shellRoot.dataset.terminalRenderer = "dom";
    }
  }

  private applyTheme() {
    const nextTheme = deriveTerminalTheme();
    this.terminal.options.theme = nextTheme.renderer;
    this.terminal.options.fontFamily = readTerminalFontFamily();
    this.terminal.options.fontSize = readTerminalFontSize();
    this.setSnapshot({ theme: nextTheme });
    this.scheduleResize();
  }

  private scheduleResize() {
    if (this.resizeTimer !== null) {
      window.clearTimeout(this.resizeTimer);
    }
    this.resizeTimer = window.setTimeout(() => {
      this.resizeTimer = null;
      if (this.disposed || !this.attachedHost) return;
      if (this.attachedHost.clientWidth < 8 || this.attachedHost.clientHeight < 8) return;
      this.terminal.options.theme = this.snapshot.theme.renderer;
      this.terminal.options.fontFamily = readTerminalFontFamily();
      this.terminal.options.fontSize = readTerminalFontSize();
      try {
        this.fitAddon.fit();
      } catch {
        return;
      }
      const next = { cols: this.terminal.cols, rows: this.terminal.rows };
      const last = this.lastSize;
      if (!last || last.cols !== next.cols || last.rows !== next.rows) {
        this.lastSize = next;
        const socket = this.socket;
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "resize", ...next }));
        }
      }
    }, RESIZE_DEBOUNCE_MS);
  }

  private disconnectResizeObserver() {
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
  }

  private async connect(create: boolean) {
    const generation = ++this.connectGeneration;
    const previousSocket = this.socket;
    this.socket = null;
    previousSocket?.close();
    this.setSnapshot({ message: "Connecting…", status: "connecting" });

    try {
      const { token } = await getTerminalToken();
      if (this.disposed || generation !== this.connectGeneration) return;
      const socket = new WebSocket(terminalWsUrl(this.terminalId, token, create));
      socket.binaryType = "arraybuffer";
      this.socket = socket;
      socket.onopen = () => {
        if (!this.isCurrentSocket(socket, generation)) return;
        this.setSnapshot({ message: "Live shell", status: "live" });
        this.focus();
        this.scheduleResize();
        if (socket.readyState === WebSocket.OPEN) {
          socket.send(
            JSON.stringify({
              type: "resize",
              cols: this.terminal.cols || DEFAULT_COLS,
              rows: this.terminal.rows || DEFAULT_ROWS,
            })
          );
        }
      };
      socket.onmessage = (event) => {
        if (!this.isCurrentSocket(socket, generation)) return;
        if (typeof event.data === "string") {
          let payload: {
            message?: string;
            type?: string;
          };
          try {
            payload = JSON.parse(event.data) as {
              message?: string;
              type?: string;
            };
          } catch {
            this.setSnapshot({ message: "Terminal sent an invalid control message.", status: "error" });
            return;
          }
          if (payload.type === "missing" || payload.type === "exit") {
            this.setSnapshot({
              message: payload.message ?? "Session ended.",
              status: "ended",
            });
          } else if (payload.type === "error") {
            this.setSnapshot({
              message: payload.message ?? "Terminal failed.",
              status: "error",
            });
          }
          return;
        }
        if (event.data instanceof ArrayBuffer) {
          this.terminal.write(new Uint8Array(event.data));
        }
      };
      socket.onerror = () => {
        if (!this.isCurrentSocket(socket, generation)) return;
        this.setSnapshot({ message: "Terminal websocket failed.", status: "error" });
      };
      socket.onclose = () => {
        if (!this.isCurrentSocket(socket, generation)) return;
        if (this.snapshot.status !== "error" && this.snapshot.status !== "ended") {
          this.setSnapshot({
            message: "Session ended. Restart to launch a fresh shell.",
            status: "ended",
          });
        }
        this.socket = null;
      };
    } catch (error) {
      if (this.disposed || generation !== this.connectGeneration) return;
      this.setSnapshot({
        message: error instanceof Error ? error.message : "Terminal failed.",
        status: "error",
      });
    }
  }

  private isCurrentSocket(socket: WebSocket, generation: number) {
    return !this.disposed && generation === this.connectGeneration && this.socket === socket;
  }
}

const runtimes = new Map<string, TerminalRuntime>();

export function getTerminalRuntime(terminalId: string) {
  let runtime = runtimes.get(terminalId);
  if (!runtime) {
    runtime = new TerminalRuntime(terminalId);
    runtimes.set(terminalId, runtime);
  }
  return runtime;
}

export function disposeTerminalRuntime(terminalId: string) {
  const runtime = runtimes.get(terminalId);
  if (!runtime) return;
  runtimes.delete(terminalId);
  runtime.dispose();
}
