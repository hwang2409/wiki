// Shared PDF.js runtime: single dynamic import + worker wiring.
// Callers get a cached module handle so worker/binary is fetched once per session.

import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";

type PdfjsModule = typeof import("pdfjs-dist");

let modulePromise: Promise<PdfjsModule> | null = null;

async function loadPdfjs(): Promise<PdfjsModule> {
  if (!modulePromise) {
    modulePromise = (async () => {
      const [module, workerUrl] = await Promise.all([
        import("pdfjs-dist"),
        // Vite ships this as a real URL string (dev + build) so the worker is
        // never inlined into the main bundle.
        import("pdfjs-dist/build/pdf.worker.mjs?url"),
      ]);
      module.GlobalWorkerOptions.workerSrc = workerUrl.default;
      return module;
    })();
  }
  return modulePromise;
}

export type LoadedPdf = {
  doc: PDFDocumentProxy;
  numPages: number;
  destroy: () => Promise<void>;
};

export async function loadPdfFromUrl(url: string): Promise<LoadedPdf> {
  const module = await loadPdfjs();
  const task = module.getDocument({ url, isEvalSupported: false, disableAutoFetch: true, disableStream: false });
  const doc = await task.promise;
  return {
    doc,
    numPages: doc.numPages,
    destroy: async () => {
      await doc.destroy();
    },
  };
}

export type PageMetrics = {
  width: number;
  height: number;
};

export function pageMetrics(page: PDFPageProxy, scale = 1): PageMetrics {
  const viewport = page.getViewport({ scale });
  return { width: viewport.width, height: viewport.height };
}

export type CancellableRender = {
  promise: Promise<PageMetrics>;
  cancel: () => void;
};

export function renderPageToCanvas(
  page: PDFPageProxy,
  canvas: HTMLCanvasElement,
  scale: number,
  devicePixelRatio: number,
): CancellableRender {
  const viewport = page.getViewport({ scale });
  const outputScale = devicePixelRatio;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("2D canvas context unavailable");
  canvas.width = Math.floor(viewport.width * outputScale);
  canvas.height = Math.floor(viewport.height * outputScale);
  canvas.style.width = `${Math.floor(viewport.width)}px`;
  canvas.style.height = `${Math.floor(viewport.height)}px`;
  const transform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
  const task = page.render({
    canvasContext: context,
    viewport,
    transform: transform ?? undefined,
  });
  const promise = task.promise
    .then(() => ({ width: viewport.width, height: viewport.height }))
    .catch((error) => {
      // Cancelled renders reject with a distinctive name; swallow silently.
      if (error && (error.name === "RenderingCancelledException" || error.message?.includes("cancelled"))) {
        return { width: viewport.width, height: viewport.height };
      }
      throw error;
    });
  return { promise, cancel: () => task.cancel() };
}

export async function renderTextLayer(
  page: PDFPageProxy,
  container: HTMLElement,
  scale: number,
): Promise<void> {
  const module = await loadPdfjs();
  container.replaceChildren();
  const viewport = page.getViewport({ scale });
  const textContent = await page.getTextContent();
  const TextLayerCtor = (module as unknown as { TextLayer?: new (options: unknown) => { render: () => Promise<void> } }).TextLayer;
  if (TextLayerCtor) {
    const layer = new TextLayerCtor({
      textContentSource: textContent,
      container,
      viewport,
    });
    await layer.render();
    return;
  }
  const legacy = (module as unknown as { renderTextLayer?: (options: unknown) => { promise: Promise<void> } }).renderTextLayer;
  if (!legacy) throw new Error("PDF.js text layer API unavailable");
  await legacy({
    textContentSource: textContent,
    container,
    viewport,
  }).promise;
}

export type PageTextIndex = {
  page: number;
  text: string;
};

// Per-page extraction cap. A page that decodes to more than this many chars
// gets truncated instead of ballooning heap; compressed text streams can
// expand sharply so we cannot trust page count alone.
export const PAGE_TEXT_CHAR_LIMIT = 200_000;

// A minimal shape that lets us feed real pdf.js pages OR unit-test fakes
// into extractPageText: all we need is a streamTextContent() → ReadableStream
// whose chunks contain an `items` array.
export type StreamablePage = {
  streamTextContent: (params?: unknown) => ReadableStream<{
    items?: Array<{ str?: string }>;
  }>;
};

export async function extractPageText(
  page: StreamablePage | PDFPageProxy,
  options: { charLimit?: number } = {},
): Promise<string> {
  const charLimit = options.charLimit ?? PAGE_TEXT_CHAR_LIMIT;
  // streamTextContent() emits chunks incrementally — we read only what fits
  // in the budget and cancel the reader as soon as the cap is hit. This
  // keeps peak memory bounded regardless of the underlying page's text
  // size: a hostile page cannot force us to buffer megabytes just so we
  // can then truncate the result.
  const stream = (page as StreamablePage).streamTextContent();
  const reader = stream.getReader();
  const parts: string[] = [];
  let total = 0;
  let cancelled = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      const items = (value as { items?: Array<{ str?: string }> })?.items;
      if (items) {
        for (const item of items) {
          const raw = item?.str;
          if (typeof raw !== "string") continue;
          const remaining = charLimit - total;
          if (remaining <= 0) break;
          if (raw.length + 1 > remaining) {
            parts.push(raw.slice(0, remaining));
            total = charLimit;
            break;
          }
          parts.push(raw);
          total += raw.length + 1; // account for the join(" ") separator
        }
      }
      if (total >= charLimit) {
        cancelled = true;
        await reader.cancel();
        break;
      }
    }
  } finally {
    if (!cancelled) {
      try {
        reader.releaseLock();
      } catch {
        // Lock is already released once the reader has finished or cancelled.
      }
    }
  }
  return parts.join(" ");
}

export type FindMatch = {
  page: number;
  matchIndex: number;
};

export type FindResult = {
  matches: FindMatch[];
  truncated: boolean;
};

// Ceiling on returned matches. One object per occurrence — a query that
// hits on every word can otherwise allocate megabytes of match records
// and stall the UI when we render the badge / navigate matches.
export const FIND_MATCH_LIMIT = 5000;

export function findMatches(
  index: PageTextIndex[],
  needle: string,
  limit: number = FIND_MATCH_LIMIT,
): FindResult {
  const trimmed = needle.trim();
  if (!trimmed) return { matches: [], truncated: false };
  const lower = trimmed.toLowerCase();
  const matches: FindMatch[] = [];
  for (const entry of index) {
    const haystack = entry.text.toLowerCase();
    let cursor = 0;
    let count = 0;
    while (cursor <= haystack.length) {
      const at = haystack.indexOf(lower, cursor);
      if (at < 0) break;
      // Detect truncation by probing for one match beyond the cap before
      // pushing. Exactly `limit` matches must NOT be reported truncated
      // (that would surface a spurious "+" in the UI); only a genuine
      // (limit + 1)th match returns truncated=true.
      if (matches.length >= limit) {
        return { matches, truncated: true };
      }
      matches.push({ page: entry.page, matchIndex: count });
      cursor = at + lower.length;
      count += 1;
    }
  }
  return { matches, truncated: false };
}
