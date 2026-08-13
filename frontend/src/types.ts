export interface NoteSummary {
  id: string;
  path: string;
  title: string;
  excerpt: string;
  updated_at: string;
  note_type?: string | null;
  meta_updated?: string | null;
}

export interface AssetMeta {
  width: number;
  height: number;
  media_type: string;
  preview_base64?: string | null;
  // Source file mtime in integer ms — cache key for the IndexedDB
  // thumbnail cache (WIKI-200).
  mtime_ms?: number | null;
}

export interface Note extends NoteSummary {
  content: string;
  asset_meta?: Record<string, AssetMeta>;
}

export interface NoteDraft {
  title: string;
  path: string;
  content: string;
}

