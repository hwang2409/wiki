// Durable UI-state mirror for wiki-* localStorage keys.
//
// Purpose: the native Wiki.app's backend can bind a different loopback port
// across launches (fallback path when the stable port is taken), which
// changes the webview origin and wipes origin-scoped localStorage. A
// server-side JSON file mirrors the wiki-* keys so a fresh origin can
// restore prior UI preferences on boot.
//
// Contract:
//   - Boot: hydrateFromServer() fetches /api/ui-state BEFORE React mounts.
//     Values are written into localStorage only when the local key is absent,
//     so an existing (newer) local value always wins.
//   - Runtime: installUiStateWriteBack() debounces PUT /api/ui-state calls
//     for the wiki-* prefix. Local writes are the source of truth; the
//     server is a passive fallback.

const KEY_PREFIX = "wiki-";
const HYDRATION_TIMEOUT_MS = 1500;
const DEBOUNCE_MS = 2000;

function withTimeout<T>(promise: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("ui-state hydration timeout")), ms);
    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

export async function hydrateFromServer(): Promise<void> {
  try {
    const response = await withTimeout(fetch("/api/ui-state", { cache: "no-store" }), HYDRATION_TIMEOUT_MS);
    if (!response.ok) return;
    const payload = (await response.json()) as { entries?: Record<string, string> };
    const entries = payload?.entries;
    if (!entries || typeof entries !== "object") return;
    for (const [key, value] of Object.entries(entries)) {
      if (!key.startsWith(KEY_PREFIX)) continue;
      if (typeof value !== "string") continue;
      // Empty string is our "removed" marker (no delete route). Skip on hydrate.
      if (value === "") continue;
      if (localStorage.getItem(key) !== null) continue;
      try {
        localStorage.setItem(key, value);
      } catch {
        // quota or serialization error — skip, don't block mount
      }
    }
  } catch {
    // Backend unreachable / slow — fail open so the app still mounts.
  }
}

let pending: Record<string, string> | null = null;
let flushTimer: ReturnType<typeof setTimeout> | null = null;
let installed = false;

function scheduleFlush(): void {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    const batch = pending;
    pending = null;
    if (!batch || Object.keys(batch).length === 0) return;
    void fetch("/api/ui-state", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entries: batch }),
    }).catch(() => {
      // Best-effort — a failed sync just means the next successful write catches up.
    });
  }, DEBOUNCE_MS);
}

function enqueue(key: string, value: string): void {
  if (!pending) pending = {};
  pending[key] = value;
  scheduleFlush();
}

export function installUiStateWriteBack(): void {
  if (installed) return;
  installed = true;

  const originalSetItem = Storage.prototype.setItem;
  const originalRemoveItem = Storage.prototype.removeItem;

  Storage.prototype.setItem = function patchedSetItem(this: Storage, key: string, value: string) {
    originalSetItem.call(this, key, value);
    if (this === localStorage && typeof key === "string" && key.startsWith(KEY_PREFIX)) {
      enqueue(key, String(value));
    }
  };

  Storage.prototype.removeItem = function patchedRemoveItem(this: Storage, key: string) {
    originalRemoveItem.call(this, key);
    if (this === localStorage && typeof key === "string" && key.startsWith(KEY_PREFIX)) {
      // Empty string signals "cleared" — server keeps parity without a delete route.
      enqueue(key, "");
    }
  };
}
