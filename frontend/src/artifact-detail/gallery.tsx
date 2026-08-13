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
// mount as sized button placeholders and only swap in an <img> once they
// enter the visible band. Under the threshold every tile stays mounted so
// tiny galleries pay no observer overhead. WIKI-200.
export const GALLERY_VIRTUALIZE_THRESHOLD = 20;

// Initial seed of live tiles rendered before the observer resolves. Two
// rows of the widest desktop grid; asymmetric with any single test count
// on purpose.
const VIRTUALIZE_SEED_SIZE = 12;

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

// IntersectionObserver-based windowing.
//
// Every tile is a real <button> (a11y: tabbable, aria-labelled). Only the
// expensive <img> inside is conditionally mounted based on visibility.
// Visibility is BOUNDED: tiles that scroll out of the observer band also
// leave `visible`, so scrolling through a huge gallery keeps the live-img
// count near ~(viewport + rootMargin) instead of growing forever.
//
// Index lookup uses a WeakMap keyed by the element, populated at
// register() time so equal-length rerenders that reuse the observer still
// resolve entries back to the right index.
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
    const seed = new Set<number>();
    for (let i = 0; i < Math.min(totalTiles, VIRTUALIZE_SEED_SIZE); i += 1) seed.add(i);
    return seed;
  });
  const nodesRef = useRef(new Map<number, HTMLLIElement>());
  const indexByNodeRef = useRef(new WeakMap<Element, number>());
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
    const indexByNode = indexByNodeRef.current;
    const observer = new IntersectionObserver(
      (entries) => {
        setVisible((prev) => {
          let mutated = false;
          const next = new Set(prev);
          for (const entry of entries) {
            const index = indexByNode.get(entry.target);
            if (index === undefined) continue;
            if (entry.isIntersecting) {
              if (!next.has(index)) {
                next.add(index);
                mutated = true;
              }
            } else if (next.has(index)) {
              next.delete(index);
              mutated = true;
            }
          }
          return mutated ? next : prev;
        });
      },
      { rootMargin: "400px 0px" },
    );
    observerRef.current = observer;
    // Observe every registered node — both live and off-screen — so
    // tiles that scroll away can leave `visible` and free their <img>.
    for (const [index, node] of nodesRef.current) {
      indexByNode.set(node, index);
      observer.observe(node);
    }
    return () => {
      observer.disconnect();
      observerRef.current = null;
    };
    // We intentionally exclude `visible` from deps — the observer resolves
    // membership itself; re-creating on every visibility change would
    // thrash. totalTiles change already rebuilds.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [totalTiles, virtualize]);
  const register = (index: number) => (el: HTMLLIElement | null) => {
    if (!virtualize) return;
    const nodes = nodesRef.current;
    const indexByNode = indexByNodeRef.current;
    if (el) {
      const previous = nodes.get(index);
      if (previous && previous !== el) {
        indexByNode.delete(previous);
        observerRef.current?.unobserve(previous);
      }
      nodes.set(index, el);
      indexByNode.set(el, index);
      observerRef.current?.observe(el);
    } else {
      const existing = nodes.get(index);
      if (existing) {
        indexByNode.delete(existing);
        observerRef.current?.unobserve(existing);
        nodes.delete(index);
      }
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
  // refresh via one batch fetch (deduped by path). The cache write only
  // runs when the server-reported mtime differs from the cached one —
  // avoids rewriting identical records on every mount. WIKI-200.
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
      setMeta((prev) => {
        const next: Record<string, AssetMeta | null> = { ...prev };
        for (const path of paths) {
          // Don't clobber a value the network already produced for this
          // path — the async cache read can still land after the batch
          // fetch on a warm reload.
          if (next[path] !== undefined && next[path] !== null) continue;
          next[path] = warm[path];
        }
        return next;
      });

      const fresh = await fetchAssetMetaBatch(paths);
      if (cancelled) return;
      const nextRecords: ThumbnailRecord[] = [];
      setMeta((prev) => {
        const next: Record<string, AssetMeta | null> = { ...prev };
        for (const path of paths) {
          const value = fresh[path] ?? null;
          if (value && value.mtimeMs !== null) {
            const cachedRecord = cached.get(path);
            // Skip the write if the cached record is identical — avoids
            // hammering IDB with a redundant put on every mount.
            if (!cachedRecord || cachedRecord.mtimeMs !== value.mtimeMs) {
              nextRecords.push({
                path,
                mtimeMs: value.mtimeMs,
                width: value.width,
                height: value.height,
                previewBase64: value.previewBase64,
                storedAt: Date.now(),
              });
            }
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
              <button
                aria-label={`Open ${item.alt} in fullscreen`}
                className={`artifact-gallery-tile${loaded ? " is-loaded" : ""}${isVisible ? "" : " is-virtualized"}`}
                data-gallery-tile-visible={isVisible || undefined}
                onClick={() => setOpenIndex(index)}
                type="button"
              >
                {isVisible ? (
                  <>
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
                  </>
                ) : null}
              </button>
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
