import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ArtifactFileEntry } from "../api";
import {
  readThumbnailBatch,
  writeThumbnailBatch,
  type ThumbnailRecord,
} from "../thumbnail-cache";
import { ArtifactLightbox, type LightboxItem } from "./lightbox";

const THUMBNAIL_WIDTHS = [320, 640] as const;
const PREVIEW_FADE_MS = 380;

// Above this tile count the gallery windows the DOM — off-screen tiles
// mount as sized placeholders and only swap in an <img> once they enter
// the visible band. Under the threshold every tile stays mounted so tiny
// galleries pay no observer overhead. WIKI-200.
export const GALLERY_VIRTUALIZE_THRESHOLD = 20;

type AssetMeta = {
  width: number;
  height: number;
  previewBase64: string | null;
  mtimeMs: number | null;
};

function vaultAssetUrl(path: string, params?: Record<string, string | number>): string {
  const cleaned = path.replace(/^\/+/, "").replace(/\\/g, "/");
  const encoded = cleaned.split("/").map(encodeURIComponent).join("/");
  const query = params
    ? "?" + Object.entries(params)
        .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
        .join("&")
    : "";
  return `/api/vault/assets/${encoded}${query}`;
}

async function fetchAssetMetaBatch(paths: string[]): Promise<Record<string, AssetMeta>> {
  if (paths.length === 0) return {};
  const response = await fetch("/api/vault/asset-meta", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ paths }),
  });
  if (!response.ok) return {};
  const body = (await response.json()) as Record<string, {
    width: number;
    height: number;
    preview_base64?: string | null;
    mtime_ms?: number | null;
  }>;
  const out: Record<string, AssetMeta> = {};
  for (const [path, entry] of Object.entries(body)) {
    out[path] = {
      width: Number(entry.width) || 0,
      height: Number(entry.height) || 0,
      previewBase64: typeof entry.preview_base64 === "string" ? entry.preview_base64 : null,
      mtimeMs: typeof entry.mtime_ms === "number" ? entry.mtime_ms : null,
    };
  }
  return out;
}

function friendlyName(entry: ArtifactFileEntry): string {
  if (entry.label && entry.label.trim()) return entry.label;
  const tail = entry.path.split(/[\\/]/).pop();
  return tail || entry.path;
}

function isResizable(entry: ArtifactFileEntry): boolean {
  return /\.(png|jpe?g|webp)$/i.test(entry.path);
}

type TileLoadState = "loading" | "ready" | "error";

// IntersectionObserver-based windowing. Off-screen tiles render a sized
// placeholder (no <img>, no preview <img>, no state) so a 200-file
// gallery only pays for the ~10 tiles the user can actually see. Tiles
// that have entered once stay resolved so scrolling back doesn't
// re-download previews — the observer flips them permanently on first
// intersection.
function useVisibleTiles(
  totalTiles: number,
  virtualize: boolean,
): { visible: Set<number>; register: (index: number) => (el: HTMLLIElement | null) => void } {
  const [visible, setVisible] = useState<Set<number>>(() => {
    if (!virtualize) {
      const all = new Set<number>();
      for (let i = 0; i < totalTiles; i += 1) all.add(i);
      return all;
    }
    // Prime the first band so the initial paint isn't empty while the
    // observer resolves. Matches the desktop gallery grid width — ~10
    // tiles cover a typical viewport row × 2.
    const seed = new Set<number>();
    for (let i = 0; i < Math.min(totalTiles, 12); i += 1) seed.add(i);
    return seed;
  });
  const nodesRef = useRef(new Map<number, HTMLLIElement>());
  const observerRef = useRef<IntersectionObserver | null>(null);
  useLayoutEffect(() => {
    if (!virtualize) {
      setVisible((prev) => {
        if (prev.size === totalTiles) return prev;
        const all = new Set<number>();
        for (let i = 0; i < totalTiles; i += 1) all.add(i);
        return all;
      });
      return;
    }
    if (typeof IntersectionObserver === "undefined") {
      const all = new Set<number>();
      for (let i = 0; i < totalTiles; i += 1) all.add(i);
      setVisible(all);
      return;
    }
    // Recreate the observer with a fresh index map on every totalTiles
    // change so entries always resolve back to the correct index — we
    // rebuild here rather than mutating a shared map.
    const indexByNode = new Map<Element, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          let mutated = false;
          const next = new Set(prev);
          for (const entry of entries) {
            if (!entry.isIntersecting) continue;
            const index = indexByNode.get(entry.target);
            if (index === undefined) continue;
            if (!next.has(index)) {
              next.add(index);
              mutated = true;
            }
            observer.unobserve(entry.target);
            indexByNode.delete(entry.target);
          }
          return mutated ? next : prev;
        });
      },
      { rootMargin: "400px 0px" },
    );
    observerRef.current = observer;
    // Observe every off-screen node captured during render. The seed
    // band (visible on first paint) is already resolved so we skip it.
    for (const [index, node] of nodesRef.current) {
      if (visible.has(index)) continue;
      indexByNode.set(node, index);
      observer.observe(node);
    }
    return () => {
      observer.disconnect();
      observerRef.current = null;
      indexByNode.clear();
    };
    // We intentionally exclude `visible` from deps — reinitializing the
    // observer every time a tile resolves would thrash. The observer
    // itself removes resolved nodes via unobserve().
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalTiles, virtualize]);
  const register = (index: number) => (el: HTMLLIElement | null) => {
    if (!virtualize) return;
    if (el) {
      nodesRef.current.set(index, el);
      const observer = observerRef.current;
      if (observer && !visible.has(index)) observer.observe(el);
    } else {
      const existing = nodesRef.current.get(index);
      if (existing && observerRef.current) observerRef.current.unobserve(existing);
      nodesRef.current.delete(index);
    }
  };
  return { visible, register };
}

export function ImageGallery({ files }: { files: ArtifactFileEntry[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [meta, setMeta] = useState<Record<string, AssetMeta | null>>({});
  const [tileState, setTileState] = useState<Record<number, TileLoadState>>({});
  const [previewMounted, setPreviewMounted] = useState<Record<number, boolean>>(() => {
    const initial: Record<number, boolean> = {};
    files.forEach((_, index) => {
      initial[index] = true;
    });
    return initial;
  });
  const fadeTimers = useRef<Map<number, number>>(new Map());
  const virtualize = files.length > GALLERY_VIRTUALIZE_THRESHOLD;
  const { visible, register } = useVisibleTiles(files.length, virtualize);

  // Hydrate from the IndexedDB thumbnail cache FIRST so blur-up
  // placeholders paint on the initial frame after a cold start; then
  // refresh via one batch fetch (deduped by path). If the server
  // returns a different mtime, replace the cached record. WIKI-200.
  useEffect(() => {
    const paths = Array.from(new Set(files.map((entry) => entry.path)));
    if (paths.length === 0) return;
    let cancelled = false;
    void (async () => {
      const cached = await readThumbnailBatch(paths);
      if (cancelled) return;
      const warm: Record<string, AssetMeta | null> = {};
      for (const path of paths) {
        const record = cached.get(path);
        warm[path] = record
          ? {
              width: record.width,
              height: record.height,
              previewBase64: record.previewBase64,
              mtimeMs: record.mtimeMs,
            }
          : null;
      }
      setMeta((prev) => ({ ...prev, ...warm }));

      const fresh = await fetchAssetMetaBatch(paths);
      if (cancelled) return;
      const nextRecords: ThumbnailRecord[] = [];
      setMeta((prev) => {
        const next: Record<string, AssetMeta | null> = { ...prev };
        for (const path of paths) {
          const value = fresh[path] ?? null;
          if (value && value.mtimeMs !== null) {
            nextRecords.push({
              path,
              mtimeMs: value.mtimeMs,
              width: value.width,
              height: value.height,
              previewBase64: value.previewBase64,
              storedAt: Date.now(),
            });
          }
          next[path] = value;
        }
        return next;
      });
      if (nextRecords.length > 0) void writeThumbnailBatch(nextRecords);
    })();
    return () => {
      cancelled = true;
    };
  }, [files]);

  useEffect(() => {
    const timers = fadeTimers.current;
    return () => {
      for (const id of timers.values()) window.clearTimeout(id);
      timers.clear();
    };
  }, []);

  const markLoaded = (index: number) => {
    setTileState((prev) => ({ ...prev, [index]: "ready" }));
    const existing = fadeTimers.current.get(index);
    if (existing) window.clearTimeout(existing);
    const timer = window.setTimeout(() => {
      setPreviewMounted((prev) => ({ ...prev, [index]: false }));
      fadeTimers.current.delete(index);
    }, PREVIEW_FADE_MS);
    fadeTimers.current.set(index, timer);
  };

  const items: LightboxItem[] = useMemo(
    () =>
      files.map((entry) => {
        const info = meta[entry.path];
        return {
          src: vaultAssetUrl(entry.path),
          alt: friendlyName(entry),
          caption: entry.label ?? entry.path,
          downloadName: entry.path.split(/[\\/]/).pop() ?? null,
          width: info?.width ?? null,
          height: info?.height ?? null,
        };
      }),
    [files, meta],
  );

  return (
    <div className="artifact-gallery" data-artifact-gallery-virtualized={virtualize || undefined}>
      <ul className="artifact-gallery-grid">
        {items.map((item, index) => {
          const entry = files[index];
          const info = meta[entry.path];
          const canResize = isResizable(entry);
          const thumbnailSrc = canResize
            ? vaultAssetUrl(entry.path, { w: THUMBNAIL_WIDTHS[0] })
            : item.src;
          const srcSet = canResize
            ? THUMBNAIL_WIDTHS.map(
                (width) => `${vaultAssetUrl(entry.path, { w: width })} ${width}w`,
              ).join(", ")
            : undefined;
          // Tiles use a fixed 4/3 ratio locked by CSS — the metadata race is
          // deliberately not allowed to swap the tile's aspect ratio. The
          // sharp source is `object-fit: cover` cropped inside that box so
          // the ratio doesn't matter visually. Real dimensions still flow
          // through to the lightbox via items[index].width/height.
          const loaded = tileState[index] === "ready";
          const showPreview = previewMounted[index] !== false;
          const isVisible = !virtualize || visible.has(index);
          return (
            <li
              className="artifact-gallery-cell"
              data-gallery-cell-index={index}
              data-gallery-cell-visible={isVisible || undefined}
              key={`${item.src}-${index}`}
              ref={register(index)}
            >
              {isVisible ? (
                <button
                  aria-label={`Open ${item.alt} in fullscreen`}
                  className={`artifact-gallery-tile${loaded ? " is-loaded" : ""}`}
                  onClick={() => setOpenIndex(index)}
                  type="button"
                >
                  {showPreview && info?.previewBase64 ? (
                    <img
                      aria-hidden="true"
                      alt=""
                      className={`artifact-gallery-preview${loaded ? " is-fading" : ""}`}
                      decoding="sync"
                      src={info.previewBase64}
                    />
                  ) : null}
                  <img
                    alt={item.alt}
                    className={`artifact-gallery-image${loaded ? " is-loaded" : ""}`}
                    decoding="async"
                    height={info?.height}
                    loading="lazy"
                    sizes="(min-width: 480px) 200px, 160px"
                    src={thumbnailSrc}
                    srcSet={srcSet}
                    width={info?.width}
                    onLoad={() => markLoaded(index)}
                    onError={() =>
                      setTileState((prev) => ({ ...prev, [index]: "error" }))
                    }
                  />
                </button>
              ) : (
                <div
                  aria-hidden="true"
                  className="artifact-gallery-tile is-virtualized"
                />
              )}
              <span className="artifact-gallery-caption" title={item.caption ?? undefined}>
                {friendlyName(entry)}
              </span>
            </li>
          );
        })}
      </ul>
      {openIndex !== null ? (
        <ArtifactLightbox
          index={openIndex}
          items={items}
          onClose={() => setOpenIndex(null)}
          onIndexChange={setOpenIndex}
        />
      ) : null}
    </div>
  );
}
