import { useEffect, useMemo, useRef, useState } from "react";
import type { ArtifactFileEntry } from "../api";
import { ArtifactLightbox, type LightboxItem } from "./lightbox";

const THUMBNAIL_WIDTHS = [320, 640] as const;
const PREVIEW_FADE_MS = 380;

type AssetMeta = {
  width: number;
  height: number;
  previewBase64: string | null;
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
  }>;
  const out: Record<string, AssetMeta> = {};
  for (const [path, entry] of Object.entries(body)) {
    out[path] = {
      width: Number(entry.width) || 0,
      height: Number(entry.height) || 0,
      previewBase64: typeof entry.preview_base64 === "string" ? entry.preview_base64 : null,
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

  // Single batch fetch on mount (or when file list changes). One request
  // per gallery, not one per tile — no per-image race.
  useEffect(() => {
    const paths = Array.from(new Set(files.map((entry) => entry.path)));
    if (paths.length === 0) return;
    let cancelled = false;
    fetchAssetMetaBatch(paths).then((batch) => {
      if (cancelled) return;
      setMeta((prev) => {
        const next: Record<string, AssetMeta | null> = { ...prev };
        for (const path of paths) {
          next[path] = batch[path] ?? null;
        }
        return next;
      });
    });
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
    <div className="artifact-gallery">
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
          return (
            <li className="artifact-gallery-cell" key={`${item.src}-${index}`}>
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
                  className="artifact-gallery-image"
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
