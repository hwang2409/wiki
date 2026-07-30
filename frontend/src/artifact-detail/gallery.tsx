import { useMemo, useState } from "react";
import type { ArtifactFileEntry } from "../api";
import { ArtifactLightbox, type LightboxItem } from "./lightbox";

function vaultAssetUrl(path: string): string {
  const cleaned = path.replace(/^\/+/, "").replace(/\\/g, "/");
  return `/api/vault/assets/${cleaned.split("/").map(encodeURIComponent).join("/")}`;
}

function friendlyName(entry: ArtifactFileEntry): string {
  if (entry.label && entry.label.trim()) return entry.label;
  const tail = entry.path.split(/[\\/]/).pop();
  return tail || entry.path;
}

export function ImageGallery({ files }: { files: ArtifactFileEntry[] }) {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const items: LightboxItem[] = useMemo(
    () =>
      files.map((entry) => ({
        src: vaultAssetUrl(entry.path),
        alt: friendlyName(entry),
        caption: entry.label ?? entry.path,
        downloadName: entry.path.split(/[\\/]/).pop() ?? null,
      })),
    [files],
  );
  return (
    <div className="artifact-gallery">
      <ul className="artifact-gallery-grid">
        {items.map((item, index) => (
          <li className="artifact-gallery-cell" key={`${item.src}-${index}`}>
            <button
              aria-label={`Open ${item.alt} in fullscreen`}
              className="artifact-gallery-tile"
              onClick={() => setOpenIndex(index)}
              type="button"
            >
              <img
                alt={item.alt}
                className="artifact-gallery-image"
                decoding="async"
                loading="lazy"
                src={item.src}
              />
            </button>
            <span className="artifact-gallery-caption" title={item.caption ?? undefined}>
              {friendlyName(files[index])}
            </span>
          </li>
        ))}
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
