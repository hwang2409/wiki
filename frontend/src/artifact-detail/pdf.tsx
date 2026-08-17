import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronsLeft,
  ChevronsRight,
  ChevronLeft,
  ChevronRight,
  Maximize2,
  Minus,
  Plus,
  RotateCcw,
  Search,
  X,
} from "lucide-react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import type { SessionArtifact, SessionEvent } from "../api";
import type { ArtifactViewState } from "../transcript-store";
import { ArtifactError, ArtifactPlaceholder } from "../artifact-state";
import { artifactUrl, usePdfDocument } from "../artifact-renderers";
import {
  extractPageText,
  findMatches,
  pageMetrics,
  renderPageToCanvas,
  renderTextLayer,
  type FindMatch,
  type PageTextIndex,
} from "../pdfjs-runtime";
import {
  applyKeyNav,
  focalPreservedScroll,
  resolveKeyNav,
  resolveZoom,
  snapToZoomStep,
  type ZoomMode,
} from "../pdf-nav";

const TEXT_INDEX_BATCH_MS = 40;
// Cap indexing so a huge PDF can't stall the worker or balloon retained
// memory. Beyond this the find overlay reports the truncation and only
// searches the first N pages.
const TEXT_INDEX_PAGE_BUDGET = 500;
// Cumulative extracted text ceiling. A page-only budget doesn't help when
// compressed streams decode to megabytes each — cap total chars too so the
// text index cannot grow beyond a few MB regardless of page density.
const TEXT_INDEX_CHAR_BUDGET = 4_000_000;

export function PdfArtifactDetail({
  artifact: _artifact,
  event,
  onChange,
  state,
  ticket,
}: {
  artifact: SessionArtifact;
  event: SessionEvent;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
  ticket: string;
}) {
  const url = artifactUrl(ticket, event);
  const { state: loadState, reload } = usePdfDocument(url);
  const [page, setPage] = useState(1);
  const [zoomMode, setZoomMode] = useState<ZoomMode>("fit-width");
  const [zoom, setZoom] = useState(1);
  const [findOpen, setFindOpen] = useState(false);
  const [findValue, setFindValue] = useState("");
  const [textIndex, setTextIndex] = useState<PageTextIndex[]>([]);
  const [indexingState, setIndexingState] = useState<"idle" | "building" | "ready">("idle");
  const [indexedPageCount, setIndexedPageCount] = useState(0);
  // Track the doc handle we've *completed* indexing for. Setting only on
  // completion means a cancelled indexing pass leaves the ref unchanged, so
  // a rapid tab-switch back to the same doc restarts cleanly instead of
  // getting stuck.
  const indexedDocRef = useRef<PDFDocumentProxy | null>(null);
  const [currentMatch, setCurrentMatch] = useState<number>(0);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const pendingFocal = useRef<{ scrollLeft: number; scrollTop: number } | null>(null);
  const previousZoom = useRef(1);
  const [pageBaseSize, setPageBaseSize] = useState<{ width: number; height: number } | null>(null);
  const numPages = loadState.status === "ready" ? loadState.pdf.numPages : 0;

  useEffect(() => {
    setPage(1);
    setTextIndex([]);
    setIndexingState("idle");
    setIndexedPageCount(0);
    setPageBaseSize(null);
    // Deliberately do NOT reset indexedDocRef here. The ref is keyed by
    // document handle, so a stale value is either replaced when the new doc
    // loads (mismatch → restart) or ignored on the same doc. Clearing it
    // here would race with a pending loadState update carrying the old doc.
  }, [url]);

  const handleReload = useCallback(() => {
    setPage(1);
    setPageBaseSize(null);
    reload();
  }, [reload]);

  useEffect(() => {
    if (loadState.status !== "ready") return;
    let cancelled = false;
    (async () => {
      let pdfPage: import("pdfjs-dist").PDFPageProxy | null = null;
      try {
        pdfPage = await loadState.pdf.doc.getPage(page);
        if (cancelled) return;
        const baseline = pageMetrics(pdfPage, 1);
        setPageBaseSize(baseline);
      } finally {
        pdfPage?.cleanup?.();
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadState, page]);

  useEffect(() => {
    if (loadState.status !== "ready" || !pageBaseSize) return;
    const viewport = viewportRef.current?.getBoundingClientRect();
    if (!viewport) return;
    const nextZoom = resolveZoom(zoomMode, { width: viewport.width, height: viewport.height }, pageBaseSize);
    setZoom(nextZoom);
  }, [loadState, pageBaseSize, zoomMode]);

  useEffect(() => {
    if (loadState.status !== "ready" || !pageBaseSize) return;
    if (!viewportRef.current) return;
    const observer = new ResizeObserver(() => {
      if (!viewportRef.current) return;
      const box = viewportRef.current.getBoundingClientRect();
      if (typeof zoomMode === "string") {
        const nextZoom = resolveZoom(zoomMode, { width: box.width, height: box.height }, pageBaseSize);
        setZoom(nextZoom);
      }
    });
    observer.observe(viewportRef.current);
    return () => observer.disconnect();
  }, [loadState, pageBaseSize, zoomMode]);

  useEffect(() => {
    if (loadState.status !== "ready" || !canvasRef.current || !textLayerRef.current || !pageBaseSize) return;
    let cancelled = false;
    let canvasRender: { cancel: () => void } | null = null;
    let textLayerRender: { cancel: () => void } | null = null;
    const canvas = canvasRef.current;
    const textLayer = textLayerRef.current;
    (async () => {
      let pdfPage: import("pdfjs-dist").PDFPageProxy | null = null;
      try {
        pdfPage = await loadState.pdf.doc.getPage(page);
        if (cancelled) return;
        const render = renderPageToCanvas(pdfPage, canvas, zoom, window.devicePixelRatio || 1);
        canvasRender = render;
        await render.promise;
        if (cancelled) return;
        textLayer.style.width = `${Math.floor(pageBaseSize.width * zoom)}px`;
        textLayer.style.height = `${Math.floor(pageBaseSize.height * zoom)}px`;
        const layerTask = renderTextLayer(pdfPage, textLayer, zoom);
        textLayerRender = layerTask;
        try {
          await layerTask.promise;
        } catch {
          textLayer.replaceChildren();
        }
        if (cancelled) return;
        if (pendingFocal.current && viewportRef.current) {
          viewportRef.current.scrollLeft = pendingFocal.current.scrollLeft;
          viewportRef.current.scrollTop = pendingFocal.current.scrollTop;
          pendingFocal.current = null;
        }
        previousZoom.current = zoom;
      } finally {
        // Release the page proxy in every exit path, including cancellation
        // and render-error, so pdf.js doesn't retain the previously-shown
        // page across every re-render.
        pdfPage?.cleanup?.();
      }
    })();
    return () => {
      cancelled = true;
      canvasRender?.cancel();
      // Cancel the text-layer stream reader too so switching pages while a
      // hostile-size text layer is streaming stops it immediately instead
      // of decoding to the char budget in the background.
      textLayerRender?.cancel();
    };
  }, [loadState, page, zoom, pageBaseSize]);

  useEffect(() => {
    if (!findOpen) return;
    if (loadState.status !== "ready") return;
    const doc = loadState.pdf.doc;
    // Same doc + already indexed → nothing to do. A tab-switch to a different
    // PDF gives a different doc handle, so this check will miss and restart.
    if (indexedDocRef.current === doc) return;
    let cancelled = false;
    setTextIndex([]);
    setIndexingState("building");
    setIndexedPageCount(0);
    (async () => {
      const accumulator: PageTextIndex[] = [];
      const pageLimit = Math.min(doc.numPages, TEXT_INDEX_PAGE_BUDGET);
      let totalChars = 0;
      let indexedPages = 0;
      for (let index = 1; index <= pageLimit; index += 1) {
        if (cancelled) return;
        let target: import("pdfjs-dist").PDFPageProxy | null = null;
        let text = "";
        try {
          target = await doc.getPage(index);
          text = await extractPageText(target);
        } catch (err) {
          // A single unreadable page mustn't kill the whole index — surface
          // it as an empty entry so page numbering stays consistent.
          console.warn(`[pdf] text extraction failed on page ${index}`, err);
        } finally {
          // Always release the page proxy, including on cancellation or
          // extraction error, otherwise pdf.js retains every page it touched.
          target?.cleanup?.();
        }
        if (cancelled) return;
        accumulator.push({ page: index, text });
        indexedPages = index;
        totalChars += text.length;
        if (totalChars >= TEXT_INDEX_CHAR_BUDGET) {
          break; // char budget wins over page budget for very dense docs
        }
        // Yield to the event loop between batches so large PDFs stay
        // responsive; the find field remains editable while indexing runs.
        if (index % 8 === 0) {
          await new Promise((resolve) => setTimeout(resolve, TEXT_INDEX_BATCH_MS));
        }
      }
      if (cancelled) return;
      setTextIndex(accumulator);
      setIndexedPageCount(indexedPages);
      setIndexingState("ready");
      // Mark THIS doc as fully indexed only after completion. A cancelled
      // pass leaves the ref alone so a resume can re-enter this effect.
      indexedDocRef.current = doc;
    })();
    return () => {
      cancelled = true;
    };
  }, [findOpen, loadState]);

  const matchResult = useMemo(
    () =>
      findValue && indexingState === "ready"
        ? findMatches(textIndex, findValue)
        : { matches: [] as FindMatch[], truncated: false },
    [findValue, indexingState, textIndex],
  );
  const matches = matchResult.matches;
  const matchesTruncated = matchResult.truncated;

  useEffect(() => {
    if (!matches.length) {
      setCurrentMatch(0);
      return;
    }
    setCurrentMatch((value) => Math.min(value, matches.length - 1));
  }, [matches.length]);

  useEffect(() => {
    if (!matches.length) return;
    const target = matches[currentMatch];
    if (target && target.page !== page) setPage(target.page);
  }, [currentMatch, matches, page]);

  const goPrev = useCallback(() => setPage((value) => Math.max(1, value - 1)), []);
  const goNext = useCallback(
    () => setPage((value) => Math.min(numPages || value, value + 1)),
    [numPages],
  );

  useEffect(() => {
    function onKeyDown(nativeEvent: KeyboardEvent) {
      if (!viewportRef.current || !viewportRef.current.matches(":focus-within, :hover")) return;
      const target = nativeEvent.target as HTMLElement | null;
      const isEditable = Boolean(
        target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable),
      );
      const intent = resolveKeyNav({
        key: nativeEvent.key,
        metaKey: nativeEvent.metaKey,
        ctrlKey: nativeEvent.ctrlKey,
        altKey: nativeEvent.altKey,
        shiftKey: nativeEvent.shiftKey,
        targetIsEditable: isEditable,
      });
      if (!intent) return;
      nativeEvent.preventDefault();
      if (intent.kind === "open-find") {
        setFindOpen(true);
        return;
      }
      setPage((value) => applyKeyNav(intent, value, numPages || value));
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [numPages]);

  function update(next: Partial<ArtifactViewState>) {
    onChange({ ...state, ...next });
  }

  function capturePendingFocal(nextZoom: number) {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const box = viewport.getBoundingClientRect();
    pendingFocal.current = focalPreservedScroll({
      viewportWidth: box.width,
      viewportHeight: box.height,
      scrollLeft: viewport.scrollLeft,
      scrollTop: viewport.scrollTop,
      currentZoom: previousZoom.current || 1,
      nextZoom,
    });
  }

  function setZoomStep(delta: 1 | -1) {
    const nextZoomValue = snapToZoomStep(zoom, delta);
    capturePendingFocal(nextZoomValue);
    setZoomMode(nextZoomValue);
  }

  function setZoomFit(mode: "fit-width" | "fit-page") {
    setZoomMode(mode);
    // Fit modes are viewport-driven; skip focal preservation so the layout
    // fills the viewport symmetrically.
    pendingFocal.current = null;
  }

  function setZoomExact(value: number) {
    capturePendingFocal(value);
    setZoomMode(value);
  }

  if (loadState.status === "error") {
    return (
      <div className="artifact-pdf-detail">
        <ArtifactError detail={loadState.message} onRetry={handleReload} title="PDF failed to load." />
      </div>
    );
  }

  const percent = Math.round(zoom * 100);
  const totalMatches = matches.length;
  const activeMatch = totalMatches ? currentMatch + 1 : 0;

  return (
    <div className="artifact-pdf-detail" data-artifact-pdf-detail>
      <div className="artifact-detail-toolbar artifact-pdf-toolbar">
        <div className="artifact-pdf-toolbar-group">
          <button aria-label="First page" data-pdf-first="true" type="button" onClick={() => setPage(1)} disabled={page <= 1}>
            <ChevronsLeft size={14} />
          </button>
          <button aria-label="Previous page" data-pdf-prev="true" type="button" onClick={goPrev} disabled={page <= 1}>
            <ChevronLeft size={12} />
          </button>
          <span className="artifact-pdf-page-indicator tabular-nums">
            page {page} of {numPages || "…"}
          </span>
          <button aria-label="Next page" data-pdf-next="true" type="button" onClick={goNext} disabled={!numPages || page >= numPages}>
            <ChevronRight size={12} />
          </button>
          <button aria-label="Last page" data-pdf-last="true" type="button" onClick={() => numPages && setPage(numPages)} disabled={!numPages || page >= numPages}>
            <ChevronsRight size={14} />
          </button>
        </div>
        <div className="artifact-pdf-toolbar-group">
          <button aria-label="Fit width" type="button" onClick={() => setZoomFit("fit-width")} data-pdf-fit-width="true">
            fit width
          </button>
          <button aria-label="Fit page" type="button" onClick={() => setZoomFit("fit-page")} data-pdf-fit-page="true">
            <Maximize2 size={12} /> fit page
          </button>
          <button aria-label="Zoom out" type="button" onClick={() => setZoomStep(-1)} data-pdf-zoom-out="true">
            <Minus size={12} />
          </button>
          <span className="artifact-pdf-zoom-value tabular-nums">{percent}%</span>
          <button aria-label="Zoom in" type="button" onClick={() => setZoomStep(1)} data-pdf-zoom-in="true">
            <Plus size={12} />
          </button>
          <button data-panel-reset-zoom="true" type="button" onClick={() => setZoomExact(1)}>
            <RotateCcw size={12} /> 100%
          </button>
        </div>
        <div className="artifact-pdf-toolbar-group">
          <button data-code-find="true" type="button" onClick={() => setFindOpen(true)}>
            <Search size={12} /> find
          </button>
          {findOpen ? (
            <label className="artifact-code-find artifact-pdf-find">
              <Search size={12} />
              <input
                autoFocus
                aria-label="Find in PDF"
                value={findValue}
                onChange={(nativeEvent) => setFindValue(nativeEvent.target.value)}
                onKeyDown={(nativeEvent) => {
                  if (nativeEvent.key === "Enter") {
                    nativeEvent.preventDefault();
                    if (totalMatches) {
                      setCurrentMatch((value) => (value + (nativeEvent.shiftKey ? -1 : 1) + totalMatches) % totalMatches);
                    }
                  }
                }}
              />
              <span className="tabular-nums" data-pdf-find-status="true">
                {indexingState === "building" && !textIndex.length
                  ? "indexing…"
                  : totalMatches
                    ? `${activeMatch}/${totalMatches}${matchesTruncated ? "+" : ""}`
                    : "0 matches"}
              </span>
              {indexingState === "ready" && numPages > indexedPageCount ? (
                <span
                  className="artifact-pdf-find-truncated"
                  data-pdf-find-truncated="true"
                  title={`Search indexed the first ${indexedPageCount} of ${numPages} pages.`}
                >
                  first {indexedPageCount} pages
                </span>
              ) : null}
              <button
                aria-label="Close find"
                type="button"
                onClick={() => {
                  setFindOpen(false);
                  setFindValue("");
                  update({ find: "" });
                }}
              >
                <X size={11} />
              </button>
            </label>
          ) : null}
        </div>
      </div>
      <div className="artifact-pdf-detail-body">
        <PdfThumbnailSidebar
          activePage={page}
          numPages={numPages}
          onSelect={setPage}
          pdf={loadState.status === "ready" ? loadState.pdf.doc : null}
        />
        <div className="artifact-pdf-viewport" ref={viewportRef} tabIndex={0}>
          {loadState.status === "loading" ? (
            <div className="artifact-pdf-viewport-loading">
              <ArtifactPlaceholder label="Loading PDF…" shape="image" />
            </div>
          ) : null}
          {loadState.status === "ready" ? (
            <div
              className="artifact-pdf-page"
              style={pageBaseSize ? {
                width: Math.floor(pageBaseSize.width * zoom),
                height: Math.floor(pageBaseSize.height * zoom),
              } : undefined}
            >
              <canvas className="artifact-pdf-page-canvas" ref={canvasRef} />
              <div
                className="artifact-pdf-page-textlayer"
                data-pdf-textlayer="true"
                ref={textLayerRef}
              />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

// Fixed thumbnail slot geometry — button (128×140) + 6px flex gap. Sidebar
// virtualization uses this to derive the visible index range from scrollTop
// without measuring each button.
const THUMB_SLOT_HEIGHT = 154;
const THUMB_WINDOW_BUFFER = 6;

function PdfThumbnailSidebar({
  activePage,
  numPages,
  onSelect,
  pdf,
}: {
  activePage: number;
  numPages: number;
  onSelect: (page: number) => void;
  pdf: import("pdfjs-dist").PDFDocumentProxy | null;
}) {
  const sidebarRef = useRef<HTMLElement | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [viewportHeight, setViewportHeight] = useState(0);

  // Track scroll + resize so the windowed render slice keeps up. Both are
  // O(1) per frame, so we don't need to throttle further than the browser
  // already does.
  useEffect(() => {
    const el = sidebarRef.current;
    if (!el) return;
    setViewportHeight(el.clientHeight);
    setScrollTop(el.scrollTop);
    const handleScroll = () => setScrollTop(el.scrollTop);
    const observer = new ResizeObserver(() => setViewportHeight(el.clientHeight));
    observer.observe(el);
    el.addEventListener("scroll", handleScroll, { passive: true });
    return () => {
      observer.disconnect();
      el.removeEventListener("scroll", handleScroll);
    };
  }, [pdf]);

  // Auto-scroll the active thumbnail into view when the caller nudges page.
  useEffect(() => {
    const el = sidebarRef.current;
    if (!el || !numPages) return;
    const anchorTop = (activePage - 1) * THUMB_SLOT_HEIGHT;
    if (anchorTop < el.scrollTop || anchorTop + THUMB_SLOT_HEIGHT > el.scrollTop + el.clientHeight) {
      el.scrollTo({ top: Math.max(0, anchorTop - el.clientHeight / 2), behavior: "smooth" });
    }
  }, [activePage, numPages]);

  if (!pdf || !numPages) {
    return <aside className="artifact-pdf-thumbnails" aria-label="PDF thumbnails" ref={sidebarRef} />;
  }

  // Windowed render: only DOM-mount thumbnails within a small band around
  // the current scroll position. `content-visibility: auto` alone still
  // pays one DOM node per page; windowing keeps DOM cost O(viewport pages).
  const firstVisible = Math.max(0, Math.floor(scrollTop / THUMB_SLOT_HEIGHT) - THUMB_WINDOW_BUFFER);
  const lastVisibleExclusive = Math.min(
    numPages,
    Math.ceil((scrollTop + viewportHeight) / THUMB_SLOT_HEIGHT) + THUMB_WINDOW_BUFFER,
  );
  const topSpacer = firstVisible * THUMB_SLOT_HEIGHT;
  const bottomSpacer = Math.max(0, (numPages - lastVisibleExclusive) * THUMB_SLOT_HEIGHT);
  const windowed: number[] = [];
  for (let index = firstVisible; index < lastVisibleExclusive; index += 1) {
    windowed.push(index + 1);
  }

  return (
    <aside
      className="artifact-pdf-thumbnails"
      aria-label="PDF thumbnails"
      data-pdf-thumbnails
      data-pdf-thumb-window-size={windowed.length}
      ref={sidebarRef}
    >
      {topSpacer ? <div style={{ height: topSpacer, flex: "0 0 auto" }} aria-hidden /> : null}
      {windowed.map((pageNumber) => (
        <PdfThumbnail
          active={pageNumber === activePage}
          key={pageNumber}
          onSelect={onSelect}
          page={pageNumber}
          pdf={pdf}
        />
      ))}
      {bottomSpacer ? <div style={{ height: bottomSpacer, flex: "0 0 auto" }} aria-hidden /> : null}
    </aside>
  );
}

function PdfThumbnail({
  active,
  onSelect,
  page,
  pdf,
}: {
  active: boolean;
  onSelect: (page: number) => void;
  page: number;
  pdf: import("pdfjs-dist").PDFDocumentProxy;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    let renderTask: { cancel: () => void } | null = null;
    (async () => {
      let target: import("pdfjs-dist").PDFPageProxy | null = null;
      try {
        target = await pdf.getPage(page);
        if (cancelled || !canvasRef.current) return;
        const viewport = target.getViewport({ scale: 1 });
        const scale = 96 / viewport.width;
        const render = renderPageToCanvas(
          target,
          canvasRef.current,
          scale,
          window.devicePixelRatio || 1,
        );
        renderTask = render;
        await render.promise;
      } catch (err) {
        // Rendering a single thumbnail can fail (page corruption, worker
        // shutdown mid-cancel) — swallow so it doesn't bubble to React's
        // uncaught-error boundary and blank the sidebar.
        if (!cancelled) console.warn(`[pdf] thumbnail render failed on page ${page}`, err);
      } finally {
        // Always release the page proxy so pdf.js doesn't retain a proxy
        // for every thumbnail that ever mounted, including cancelled ones.
        target?.cleanup?.();
      }
    })();
    return () => {
      cancelled = true;
      renderTask?.cancel();
      // Free the canvas backing store when the thumbnail unmounts (windowing
      // rolls it off DOM) or the doc changes. Zeroing width is the standard
      // way to force browsers to drop the pixel buffer.
      if (canvas.width) {
        canvas.width = 0;
        canvas.height = 0;
      }
    };
  }, [page, pdf]);

  return (
    <button
      aria-current={active ? "true" : undefined}
      aria-label={`Go to page ${page}`}
      className={`artifact-pdf-thumbnail${active ? " is-active" : ""}`}
      data-pdf-thumb-page={page}
      onClick={() => onSelect(page)}
      type="button"
    >
      <canvas className="artifact-pdf-thumbnail-canvas" ref={canvasRef} />
      <span className="artifact-pdf-thumbnail-label tabular-nums">{page}</span>
    </button>
  );
}
