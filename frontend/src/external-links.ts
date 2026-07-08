const EXTERNAL_LINK_REL = "noopener noreferrer";

type WindowWithTauri = Window & {
  __TAURI_INTERNALS__?: unknown;
  __wikiExternalLinksInstalled__?: boolean;
};

function currentHref() {
  return typeof window === "undefined" ? "http://localhost/" : window.location.href;
}

function isTauriRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in (window as WindowWithTauri);
}

function resolveUrl(href: string, base = currentHref()) {
  try {
    return new URL(href, base);
  } catch {
    return null;
  }
}

export function isExternalHttpUrl(href?: string, base = currentHref()) {
  if (!href) return false;
  const url = resolveUrl(href, base);
  const current = resolveUrl(base, base);
  if (!url || !current) return false;
  if (url.protocol !== "http:" && url.protocol !== "https:") return false;
  return url.origin !== current.origin;
}

export function externalLinkProps(href?: string) {
  return isExternalHttpUrl(href) ? { rel: EXTERNAL_LINK_REL, target: "_blank" as const } : {};
}

async function openExternalUrl(url: string) {
  const { openUrl } = await import("@tauri-apps/plugin-opener");
  await openUrl(url);
}

function findAnchor(target: EventTarget | null) {
  return target instanceof Element ? target.closest<HTMLAnchorElement>("a[href]") : null;
}

function isPlainLeftClick(event: MouseEvent) {
  return (
    event.button === 0 &&
    !event.defaultPrevented &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.altKey
  );
}

function fallbackToRustNavigation(url: string) {
  // If the JS opener is denied or unavailable, force a normal navigation.
  // The native Tauri on_navigation/on_new_window handlers then open the URL
  // in the system browser instead of leaving the click dead.
  window.location.assign(url);
}

export function installExternalLinkInterceptors() {
  if (typeof window === "undefined" || typeof document === "undefined" || !isTauriRuntime()) return;
  const tauriWindow = window as WindowWithTauri;
  if (tauriWindow.__wikiExternalLinksInstalled__) return;
  tauriWindow.__wikiExternalLinksInstalled__ = true;

  document.addEventListener(
    "click",
    (event) => {
      if (!(event instanceof MouseEvent) || !isPlainLeftClick(event)) return;
      const anchor = findAnchor(event.target);
      if (!anchor || !isExternalHttpUrl(anchor.href)) return;
      event.preventDefault();
      void openExternalUrl(anchor.href).catch((error) => {
        console.error("native external link opener failed", error);
        fallbackToRustNavigation(anchor.href);
      });
    },
    true
  );

  const originalOpen = window.open.bind(window);
  window.open = ((url?: string | URL, target?: string, features?: string) => {
    const href = typeof url === "string" || url instanceof URL ? String(url) : "";
    if (isExternalHttpUrl(href)) {
      void openExternalUrl(href).catch((error) => {
        console.error("native window.open external opener failed", error);
        fallbackToRustNavigation(href);
      });
      return null;
    }
    return originalOpen(url, target, features);
  }) as Window["open"];
}
