import { useEffect, useMemo, useState } from "react";
import type { ArtifactFileEntry } from "../api";
import { ArtifactLightbox, type LightboxItem } from "./lightbox";

const THUMBNAIL_WIDTHS = [320, 640] as const;

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

function vaultAssetMetaUrl(path: string): string {
  const cleaned = path.replace(/^\/+/, "").replace(/\\/g, "/");
  return `/api/vault/asset-meta/${cleaned.split("/").map(encodeURIComponent).join("/")}`;
}

function friendlyName(entry: ArtifactFileEntry): string {
  if (entry.label && entry.label.trim()) return entry.label;
  const tail = entry.path.split(/[\\/]/).pop();
  return tail || entry.path;
}

function isResizable(entry: ArtifactFileEntry): boolean {
  return /\.(png|jpe?g|webp)$/i.test(entry.path);
}

export function ImageGallery({ files }: { files: ArtifactFileEntry[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const [meta, setMeta] = useState<Record<string, AssetMeta | null>>({});

  useEffect(() => {
    const controller = new AbortController();
    for (const entry of files) {
      if (meta[entry.path] !== undefined) continue;
      // eslint-disable-next-line @typescript-eslint/no-floating-promises
      fetch(vaultAssetMetaUrl(entry.path), { signal: controller.signal })
        .then((response) => (response.ok ? response.json() : null))
        .then((body) => {
          if (!body) {
            setMeta((prev) => ({ ...prev, [entry.path]: null }));
            return;
          }
          setMeta((prev) => ({
            ...prev,
            [entry.path]: {
              width: Number(body.width) || 0,
              height: Number(body.height) || 0,
              previewBase64:
                typeof body.preview_base64 === "string" ? body.preview_base64 : null,
            },
          }));
        })
        .catch(() => {
          setMeta((prev) => ({ ...prev, [entry.path]: null }));
        });
    }
    return () => controller.abort();
    // meta is intentionally omitted — we only want to fetch once per file
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files]);

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
          const knownRatio = info?.width && info?.height ? info.width / info.height : null;
          const tileStyle = knownRatio ? { aspectRatio: `${info!.width} / ${info!.height}` } : undefined;
          return (
            <li className="artifact-gallery-cell" key={`${item.src}-${index}`}>
              <button
                aria-label={`Open ${item.alt} in fullscreen`}
                className="artifact-gallery-tile"
                onClick={() => setOpenIndex(index)}
                style={tileStyle}
                type="button"
              >
                {info?.previewBase64 ? (
                  <img
                    aria-hidden="true"
                    alt=""
                    className="artifact-gallery-preview"
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
