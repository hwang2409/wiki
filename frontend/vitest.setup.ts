// jsdom 29 does not expose window.localStorage, and Node's experimental
// global localStorage requires a --localstorage-file flag we don't want to
// wire up. Install a minimal in-memory Storage shim on both `window` and
// `globalThis` so components that read/write `localStorage` work under test.

class MemoryStorage implements Storage {
  private store = new Map<string, string>();
  get length() {
    return this.store.size;
  }
  clear() {
    this.store.clear();
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(index: number) {
    return Array.from(this.store.keys())[index] ?? null;
  }
  removeItem(key: string) {
    this.store.delete(key);
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value));
  }
}

function installStorage(target: object, name: "localStorage" | "sessionStorage") {
  Object.defineProperty(target, name, {
    configurable: true,
    writable: true,
    value: new MemoryStorage(),
  });
}

if (typeof window !== "undefined") {
  installStorage(window, "localStorage");
  installStorage(window, "sessionStorage");
}
installStorage(globalThis, "localStorage");
installStorage(globalThis, "sessionStorage");

// jsdom does not implement ResizeObserver; utility pages that render charts /
// canvases rely on it. A no-op shim is enough for tests that only assert
// against surrounding chrome and data.
class NoopResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

if (typeof window !== "undefined" && typeof window.ResizeObserver === "undefined") {
  (window as unknown as { ResizeObserver: typeof NoopResizeObserver }).ResizeObserver =
    NoopResizeObserver;
}
if (typeof (globalThis as { ResizeObserver?: unknown }).ResizeObserver === "undefined") {
  (globalThis as { ResizeObserver: typeof NoopResizeObserver }).ResizeObserver =
    NoopResizeObserver;
}

// WIKI-253: jsdom returns offsetHeight = 0 for every node, which would prevent
// the height-driven collapse gate from ever firing under vitest. Element-level
// tests override via `data-mock-height` on a specific node; otherwise the
// block-preview wrapper approximates its rendered height from the text volume
// it contains (`~20px per line, char-density fallback for wrap-heavy blocks`).
// A 3-line output measures ~60px (stays inline), a 24-line output measures
// past the collapse threshold, mirroring real-browser behavior closely enough
// to exercise the gate deterministically.
if (typeof HTMLElement !== "undefined") {
  const existing = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
  if (!existing || existing.configurable !== false) {
    Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
      configurable: true,
      get() {
        const raw = (this as HTMLElement).dataset?.mockHeight;
        if (raw !== undefined) return Number(raw);
        const el = this as HTMLElement;
        if (el.classList?.contains("session-tool-block-preview-measure")) {
          const text = el.textContent ?? "";
          const lines = text.split("\n").length;
          const chars = text.length;
          return Math.max(lines * 20, Math.floor(chars / 3));
        }
        return 0;
      },
    });
  }
}
