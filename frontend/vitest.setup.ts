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

