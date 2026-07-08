type WindowWithTauri = Window & {
  __TAURI_INTERNALS__?: unknown;
  __wikiExternalLinksInstalled__?: boolean;
};

const EXTERNAL_LINK_REL = "noopener noreferrer";

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
  if (isTauriRuntime()) {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(url);
    return;
  }
  window.open(url, "_blank", "noopener,noreferrer");
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

export function installExternalLinkInterceptors() {
  if (typeof window === "undefined" || typeof document === "undefined") return;
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
      void openExternalUrl(anchor.href);
    },
    true
  );

  const originalOpen = window.open.bind(window);
  window.open = ((url?: string | URL, target?: string, features?: string) => {
    const href = typeof url === "string" || url instanceof URL ? String(url) : "";
    if (isTauriRuntime() && isExternalHttpUrl(href)) {
      void openExternalUrl(href);
      return null;
    }
    return originalOpen(url, target, features);
  }) as Window["open"];
}
