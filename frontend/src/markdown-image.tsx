import { useEffect, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { ArtifactLightbox } from "./artifact-detail/lightbox";
import {
  deleteThumbnail,
  readThumbnail,
  writeThumbnail,
  type ThumbnailRecord,
} from "./thumbnail-cache";

const vaultImageExtension = /\.(?:png|jpe?g|gif|webp|svg)$/i;

// Raw-string variant kept for callers that hand us un-split values (the
// Obsidian wikilink resolver in markdown.tsx). Splits on the LITERAL query
// / fragment delimiter first — see assetCandidates for the decode-safe
// path used on markdown image destinations.
export function isVaultImagePath(value: string) {
  return vaultImageExtension.test(value.split(/[?#]/, 1)[0]);
}

function decodeAssetPath(value: string) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function normalizeAssetPath(value: string, base: string[] = []) {
  const parts = value.replaceAll("\\", "/").split("/");
  const resolved = [...base];
  for (const part of parts) {
    if (!part || part === ".") continue;
    if (part === "..") {
      if (resolved.length > 0 && resolved[resolved.length - 1] !== "..") resolved.pop();
      else resolved.push("..");
    } else {
      resolved.push(part);
    }
  }
  return resolved.length > 0 ? resolved.join("/") : null;
}

function vaultAssetUrl(path: string) {
  return `/api/vault/assets/${path.split("/").map(encodeURIComponent).join("/")}`;
}

function assetCandidates(src: string, notePath?: string) {
  if (/^[a-z][a-z\d+.-]*:/i.test(src) || src.startsWith("//")) return [];
  // Split on the LITERAL query / fragment delimiters FIRST — mirrors the
  // backend's `_extract_note_image_paths.note_relative`. Decoding before
  // splitting would treat `hero%23draft.png` and `hero%3Fdraft.png` as
  // if they carried real `#` / `?` delimiters and silently drop the
  // filename tail; the backend would still ship metadata for those
  // files and the tile would sit forever in shimmer.
  const withoutQuery = src.split(/[?#]/, 1)[0];
  const decoded = decodeAssetPath(withoutQuery);
  if (decoded.includes("\0") || decoded.startsWith("/")) return [];
  // Check the extension directly on the decoded name — do NOT re-split
  // on `?`/`#`; any such characters here came from percent-encoded input
  // and are part of the filename.
  if (!vaultImageExtension.test(decoded)) return [];
  const rootPath = normalizeAssetPath(decoded);
  const notePathCandidate = notePath
    ? normalizeAssetPath(decoded, notePath.split("/").slice(0, -1))
    : null;
  return [notePathCandidate, rootPath].filter(
    (candidate, index, candidates): candidate is string =>
      Boolean(candidate) && candidates.indexOf(candidate) === index,
  );
}

export type MarkdownImageProps = {
  alt?: string;
  className?: string;
  "data-obsidian-width"?: number | string;
  node?: unknown;
  notePath?: string;
  src?: string;
};

function nodeProperties(node: unknown): Record<string, unknown> {
  if (!node || typeof node !== "object") return {};
  const typedNode = node as {
    data?: { hProperties?: Record<string, unknown> };
    properties?: Record<string, unknown>;
  };
  return typedNode.data?.hProperties ?? typedNode.properties ?? {};
}

type AssetMeta = {
  width: number;
  height: number;
  previewBase64: string | null;
  mtimeMs: number | null;
};

// Shared cache: successful metadata is cached forever. Failed requests do
// not write a null marker, so the next mount can retry. In-flight fetches
// are shared, and the underlying fetch is NEVER aborted from cleanup.
const assetMetaCache = new Map<string, AssetMeta>();
const assetMetaPending = new Map<string, Promise<AssetMeta | null>>();
const assetMetaHydrationGeneration = new Map<string, number>();

const MAX_META_FETCH_ATTEMPTS = 3;

function invalidateAssetMeta(path: string): void {
  assetMetaCache.delete(path);
  assetMetaHydrationGeneration.set(
    path,
    (assetMetaHydrationGeneration.get(path) ?? 0) + 1,
  );
}

function fetchAssetMeta(path: string): Promise<AssetMeta | null> {
  const cached = assetMetaCache.get(path);
  if (cached !== undefined) return Promise.resolve(cached);
  const pending = assetMetaPending.get(path);
  if (pending) return pending;
  const encoded = path.split("/").map(encodeURIComponent).join("/");
  const request = (async () => {
    for (let attempt = 0; attempt < MAX_META_FETCH_ATTEMPTS; attempt += 1) {
      try {
        const response = await fetch(`/api/vault/asset-meta/${encoded}`);
        if (response.status === 404) {
          // The asset is gone. Remove stale IDB data, but do not cache a
          // null result that would prevent a later recreated asset retry.
          invalidateAssetMeta(path);
          void deleteThumbnail(path);
          return null;
        }
        if (!response.ok) {
          invalidateAssetMeta(path);
          continue;
        }
        const body = await response.json();
        const meta: AssetMeta = {
          width: Number(body.width) || 0,
          height: Number(body.height) || 0,
          previewBase64: typeof body.preview_base64 === "string" ? body.preview_base64 : null,
          mtimeMs: typeof body.mtime_ms === "number" ? body.mtime_ms : null,
        };
        assetMetaCache.set(path, meta);
        // Persist to IndexedDB so the next cold start paints the blur-up
        // placeholder on the first frame. mtime_ms is the invalidation
        // key — a stale entry is dropped on the next request. WIKI-200.
        if (meta.mtimeMs !== null) {
          void writeThumbnail({
            path,
            mtimeMs: meta.mtimeMs,
            width: meta.width,
            height: meta.height,
            previewBase64: meta.previewBase64,
            storedAt: Date.now(),
          });
        }
        return meta;
      } catch {
        invalidateAssetMeta(path);
      }
    }
    return null;
  })()
    .finally(() => {
      assetMetaPending.delete(path);
    });
  assetMetaPending.set(path, request);
  return request;
}

// Test hook — reset the shared caches between tests. Never call from
// production code.
export function __resetAssetMetaForTests(): void {
  assetMetaCache.clear();
  assetMetaPending.clear();
  assetMetaHydrationGeneration.clear();
}

// Hydrate the in-memory asset meta cache from the IndexedDB thumbnail
// cache. Callers can await this to force cache warmth; MarkdownImage
// fires it in the background so the first frame after a cold start
// still gets a blur-up placeholder even if the network hasn't answered.
//
// A network answer that lands before this resolves ALWAYS wins — the
// hydrated record is discarded rather than written back, so a stale IDB
// entry can't overwrite fresher server data.
export async function hydrateAssetMetaFromThumbnailCache(path: string): Promise<AssetMeta | null> {
  if (assetMetaCache.get(path) !== undefined) return assetMetaCache.get(path) ?? null;
  const generation = assetMetaHydrationGeneration.get(path) ?? 0;
  const record: ThumbnailRecord | null = await readThumbnail(path);
  if (!record) return null;
  // Re-check the cache AFTER the async IDB read — the network fetch
  // may have populated it while we were waiting.
  if (assetMetaCache.get(path) !== undefined) return assetMetaCache.get(path) ?? null;
  // A rejected network request invalidates this hydration generation.
  // Do not put stale IDB metadata back into the cache after that point.
  if ((assetMetaHydrationGeneration.get(path) ?? 0) !== generation) return null;
  const meta: AssetMeta = {
    width: record.width,
    height: record.height,
    previewBase64: record.previewBase64,
    mtimeMs: record.mtimeMs,
  };
  assetMetaCache.set(path, meta);
  return meta;
}

export function seedAssetMetaCache(entries: Record<string, {
  width: number;
  height: number;
  preview_base64?: string | null;
  mtime_ms?: number | null;
}> | undefined | null): void {
  if (!entries) return;
  for (const [path, entry] of Object.entries(entries)) {
    if (!entry || typeof entry !== "object") continue;
    const mtimeMs = typeof entry.mtime_ms === "number" ? entry.mtime_ms : null;
    const meta = {
      width: Number(entry.width) || 0,
      height: Number(entry.height) || 0,
      previewBase64: typeof entry.preview_base64 === "string" ? entry.preview_base64 : null,
      mtimeMs,
    };
    assetMetaCache.set(path, meta);
    if (mtimeMs !== null) {
      void writeThumbnail({
        path,
        mtimeMs,
        width: meta.width,
        height: meta.height,
        previewBase64: meta.previewBase64,
        storedAt: Date.now(),
      });
    }
  }
}

export function assetMetaFromCache(path: string): AssetMeta | null | undefined {
  return assetMetaCache.get(path);
}

export function MarkdownImage({ alt, className, "data-obsidian-width": dataWidth, node, notePath, src }: MarkdownImageProps) {
  const candidates = src ? assetCandidates(src, notePath) : [];
  const [failed, setFailed] = useState(false);
  const activeCandidate = candidates.length > 0
    ? candidates[failed && candidates.length > 1 ? 1 : 0]
    : null;
  // Consult the cache SYNCHRONOUSLY during the first render so any dimensions
  // that arrived with the note payload (or a prior render) are on the frame
  // before React commits. Falls back to an async probe only if the cache is
  // empty for this asset — the async fetch is shared across every consumer
  // and never aborted by an individual consumer's cleanup.
  const cachedMeta = activeCandidate ? assetMetaFromCache(activeCandidate) : undefined;
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [meta, setMeta] = useState<AssetMeta | null>(cachedMeta ?? null);
  // Ratio is LOCKED once — either from meta arriving before image load, or
  // from the image's own naturalWidth/Height on load if meta hasn't shown
  // up yet. Late-arriving meta is used for the lightbox but never rewrites
  // the frame's aspect ratio, so a metadata race can't cause CLS.
  const [frameRatio, setFrameRatio] = useState<{ w: number; h: number } | null>(() =>
    cachedMeta && cachedMeta.width > 0 && cachedMeta.height > 0
      ? { w: cachedMeta.width, h: cachedMeta.height }
      : null,
  );
  const [previewMounted, setPreviewMounted] = useState(true);
  const [lightboxOpen, setLightboxOpen] = useState(false);

  useEffect(() => {
    setFailed(false);
    setState("loading");
    setPreviewMounted(true);
    // Prime meta from cache on src/notePath change; not on activeCandidate
    // change so a fallback retry (candidates[0] -> candidates[1]) doesn't
    // undo itself in an infinite loop.
    const first = candidates.length > 0 ? candidates[0] : null;
    const preloaded = first ? assetMetaFromCache(first) : undefined;
    setMeta(preloaded ?? null);
    setFrameRatio(
      preloaded && preloaded.width > 0 && preloaded.height > 0
        ? { w: preloaded.width, h: preloaded.height }
        : null,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src, notePath]);

  useEffect(() => {
    if (state !== "ready") return;
    const timer = window.setTimeout(() => setPreviewMounted(false), 380);
    return () => window.clearTimeout(timer);
  }, [state]);

  // Tracks the newest source of meta already applied. Slow IDB hydration
  // must never clobber a fresher network answer that landed first. `null`
  // = no answer yet; number = mtimeMs; `Infinity` = definitive
  // network verdict without an mtime (still wins over any IDB record).
  const metaFreshnessRef = useRef<number | null>(null);

  useEffect(() => {
    if (!activeCandidate) return;
    // Reset the freshness watermark for this candidate; a prior
    // asset's watermark must not gate the new one.
    metaFreshnessRef.current = null;
    if (assetMetaFromCache(activeCandidate) !== undefined) return;
    let cancelled = false;

    const applyMeta = (info: AssetMeta | null, source: "hydrate" | "network") => {
      if (cancelled) return;
      if (!info) {
        if (source === "network") {
          metaFreshnessRef.current = Number.POSITIVE_INFINITY;
          setMeta(null);
        }
        return;
      }
      const incoming = info.mtimeMs ?? (source === "network" ? Number.POSITIVE_INFINITY : -1);
      const current = metaFreshnessRef.current;
      // Stale IDB (older mtime OR unknown mtime) never clobbers a value
      // we've already applied. Network wins ties — a network answer with
      // the same mtime replaces the hydrated one only if it carries a
      // different preview payload, guarding against IDB drift.
      if (current !== null && incoming < current) return;
      metaFreshnessRef.current = incoming;
      setMeta((prev) => {
        if (prev && prev.mtimeMs === info.mtimeMs && prev.previewBase64 === info.previewBase64) return prev;
        return info;
      });
      if (info.width > 0 && info.height > 0) {
        setFrameRatio((prev) => prev ?? { w: info.width, h: info.height });
      }
    };

    // Hydrate synchronously from IndexedDB first — this puts a blur-up
    // preview on the frame BEFORE the network answers on a cold start.
    // The follow-up network fetch still runs; if the server returns a
    // fresher preview (different mtime), fetchAssetMeta writes over
    // both caches and the component re-renders with the new preview.
    void hydrateAssetMetaFromThumbnailCache(activeCandidate).then((warm) => {
      applyMeta(warm, "hydrate");
    });
    fetchAssetMeta(activeCandidate).then((info) => {
      applyMeta(info, "network");
    });
    return () => {
      cancelled = true;
    };
  }, [activeCandidate]);

  const currentSrc = src && activeCandidate ? vaultAssetUrl(activeCandidate) : src;
  const widthValue = dataWidth ?? nodeProperties(node)["data-obsidian-width"];
  const explicitWidth = typeof widthValue === "number" ? widthValue : undefined;

  if (!currentSrc) return null;

  const knownRatio = frameRatio ? frameRatio.w / frameRatio.h : null;

  const frameStyle: CSSProperties = {};
  if (explicitWidth) frameStyle.width = `${explicitWidth}px`;
  if (frameRatio) frameStyle.aspectRatio = `${frameRatio.w} / ${frameRatio.h}`;

  const label = alt || src?.split(/[\\/]/).pop() || "Image";

  return (
    <>
      <button
        aria-label={`Open ${label} in fullscreen`}
        className={`markdown-image-frame${state === "ready" ? " is-loaded" : ""}${state === "error" ? " is-error" : ""}${knownRatio ? " has-known-ratio" : ""}`}
        onClick={() => {
          if (state === "ready") setLightboxOpen(true);
        }}
        style={frameStyle}
        type="button"
      >
        {previewMounted && meta?.previewBase64 ? (
          <img
            aria-hidden="true"
            alt=""
            className={`markdown-image-preview${state === "ready" ? " is-fading" : ""}`}
            decoding="sync"
            src={meta.previewBase64}
          />
        ) : state === "loading" ? (
          <span className="markdown-image-shimmer" aria-hidden="true" />
        ) : null}
        <img
          alt={alt ?? ""}
          className={className}
          decoding="async"
          height={meta?.height}
          loading="lazy"
          src={currentSrc}
          style={explicitWidth ? { width: `${explicitWidth}px` } : undefined}
          width={meta?.width}
          onError={() => {
            if (candidates.length > 1 && !failed) {
              setFailed(true);
              return;
            }
            // Only flip to the fallback overlay for vault-relative sources —
            // external URLs stay in the DOM so consumers can still inspect
            // them and let the browser render the native broken-image icon.
            if (candidates.length > 0) setState("error");
          }}
          onLoad={(event) => {
            const img = event.currentTarget;
            // Fallback ratio lock: if metadata hasn't shown up yet, use the
            // image's own naturalWidth/Height so the frame stays put when
            // meta arrives later.
            if (img.naturalWidth > 0 && img.naturalHeight > 0) {
              setFrameRatio((prev) => prev ?? { w: img.naturalWidth, h: img.naturalHeight });
            }
            setState("ready");
          }}
        />
        {state === "error" ? (
          <span className="markdown-image-error" role="img" aria-label={`Image failed to load: ${label}`}>
            image unavailable
          </span>
        ) : null}
      </button>
      {lightboxOpen ? (
        <ArtifactLightbox
          index={0}
          items={[{
            src: currentSrc,
            alt: label,
            caption: alt || label,
            width: meta?.width,
            height: meta?.height,
          }]}
          onClose={() => setLightboxOpen(false)}
          onIndexChange={() => undefined}
        />
      ) : null}
    </>
  );
}
