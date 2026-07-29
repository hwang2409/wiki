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

export async function extractPageText(page: PDFPageProxy): Promise<string> {
  const content = await page.getTextContent();
  return content.items
    .map((item) => (typeof (item as { str?: string }).str === "string" ? (item as { str: string }).str : ""))
    .join(" ");
}

export type FindMatch = {
  page: number;
  matchIndex: number;
};

export function findMatches(index: PageTextIndex[], needle: string): FindMatch[] {
  const trimmed = needle.trim();
  if (!trimmed) return [];
  const lower = trimmed.toLowerCase();
  const matches: FindMatch[] = [];
  for (const entry of index) {
    const haystack = entry.text.toLowerCase();
    let cursor = 0;
    let count = 0;
    while (cursor <= haystack.length) {
      const at = haystack.indexOf(lower, cursor);
      if (at < 0) break;
      matches.push({ page: entry.page, matchIndex: count });
      cursor = at + lower.length;
      count += 1;
    }
  }
  return matches;
}
