import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  Maximize2,
  Minus,
  Plus,
  RotateCcw,
  Search,
  X,
} from "lucide-react";
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
  const indexingStartedRef = useRef(false);
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
    setPageBaseSize(null);
    indexingStartedRef.current = false;
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
      const pdfPage = await loadState.pdf.doc.getPage(page);
      if (cancelled) return;
      const baseline = pageMetrics(pdfPage, 1);
      setPageBaseSize(baseline);
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
    let active: { cancel: () => void } | null = null;
    const canvas = canvasRef.current;
    const textLayer = textLayerRef.current;
    (async () => {
      const pdfPage = await loadState.pdf.doc.getPage(page);
      if (cancelled) return;
      const render = renderPageToCanvas(pdfPage, canvas, zoom, window.devicePixelRatio || 1);
      active = render;
      await render.promise;
      if (cancelled) return;
      textLayer.style.width = `${Math.floor(pageBaseSize.width * zoom)}px`;
      textLayer.style.height = `${Math.floor(pageBaseSize.height * zoom)}px`;
      try {
        await renderTextLayer(pdfPage, textLayer, zoom);
      } catch {
        textLayer.replaceChildren();
      }
      if (pendingFocal.current && viewportRef.current) {
        viewportRef.current.scrollLeft = pendingFocal.current.scrollLeft;
        viewportRef.current.scrollTop = pendingFocal.current.scrollTop;
        pendingFocal.current = null;
      }
      previousZoom.current = zoom;
    })();
    return () => {
      cancelled = true;
      active?.cancel();
    };
  }, [loadState, page, zoom, pageBaseSize]);

  useEffect(() => {
    if (!findOpen) return;
    if (loadState.status !== "ready") return;
    if (indexingStartedRef.current) return;
    indexingStartedRef.current = true;
    setIndexingState("building");
    let cancelled = false;
    (async () => {
      const accumulator: PageTextIndex[] = [];
      const doc = loadState.pdf.doc;
      for (let index = 1; index <= doc.numPages; index += 1) {
        if (cancelled) return;
        const target = await doc.getPage(index);
        const text = await extractPageText(target);
        // Release the page early; keeping every page pinned costs memory.
        target.cleanup?.();
        accumulator.push({ page: index, text });
        // Yield to the event loop between batches so large PDFs stay
        // responsive; the find field remains editable while indexing runs.
        if (index % 8 === 0) {
          await new Promise((resolve) => setTimeout(resolve, TEXT_INDEX_BATCH_MS));
        }
      }
      if (cancelled) return;
      setTextIndex(accumulator);
      setIndexingState("ready");
    })();
    return () => {
      cancelled = true;
    };
  }, [findOpen, loadState]);

  const matches = useMemo<FindMatch[]>(
    () => (findValue && indexingState === "ready" ? findMatches(textIndex, findValue) : []),
    [findValue, indexingState, textIndex],
  );

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
            «
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
            »
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
                    ? `${activeMatch}/${totalMatches}`
                    : "0 matches"}
              </span>
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
  const observerRef = useRef<IntersectionObserver | null>(null);
  const visibility = useRef<Map<number, () => void>>(new Map());
  const [visibleSet, setVisibleSet] = useState<ReadonlySet<number>>(() => new Set());

  useEffect(() => {
    if (!sidebarRef.current) return;
    const map = visibility.current;
    const observer = new IntersectionObserver(
      (entries) => {
        setVisibleSet((current) => {
          const next = new Set(current);
          for (const entry of entries) {
            const raw = (entry.target as HTMLElement).dataset.pdfThumbPage;
            const pageNumber = raw ? Number(raw) : NaN;
            if (Number.isNaN(pageNumber)) continue;
            if (entry.isIntersecting) next.add(pageNumber);
          }
          return next;
        });
      },
      { root: sidebarRef.current, rootMargin: "200px" },
    );
    observerRef.current = observer;
    return () => {
      observer.disconnect();
      map.clear();
      observerRef.current = null;
    };
  }, []);

  const observeCallback = useCallback((element: HTMLElement | null, pageNumber: number) => {
    if (!observerRef.current) return;
    if (element) {
      element.dataset.pdfThumbPage = String(pageNumber);
      observerRef.current.observe(element);
    }
  }, []);

  if (!pdf || !numPages) {
    return <aside className="artifact-pdf-thumbnails" aria-label="PDF thumbnails" ref={sidebarRef} />;
  }
  return (
    <aside className="artifact-pdf-thumbnails" aria-label="PDF thumbnails" ref={sidebarRef}>
      {Array.from({ length: numPages }, (_, index) => index + 1).map((pageNumber) => (
        <PdfThumbnail
          active={pageNumber === activePage}
          key={pageNumber}
          onObserve={observeCallback}
          onSelect={onSelect}
          page={pageNumber}
          pdf={pdf}
          visible={visibleSet.has(pageNumber)}
        />
      ))}
    </aside>
  );
}

function PdfThumbnail({
  active,
  onObserve,
  onSelect,
  page,
  pdf,
  visible,
}: {
  active: boolean;
  onObserve: (element: HTMLElement | null, page: number) => void;
  onSelect: (page: number) => void;
  page: number;
  pdf: import("pdfjs-dist").PDFDocumentProxy;
  visible: boolean;
}) {
  const wrapperRef = useRef<HTMLButtonElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    onObserve(wrapperRef.current, page);
  }, [onObserve, page]);

  useEffect(() => {
    if (!visible || !canvasRef.current) return;
    let cancelled = false;
    let active: { cancel: () => void } | null = null;
    (async () => {
      const target = await pdf.getPage(page);
      if (cancelled || !canvasRef.current) return;
      const viewport = target.getViewport({ scale: 1 });
      const scale = 96 / viewport.width;
      const render = renderPageToCanvas(target, canvasRef.current, scale, window.devicePixelRatio || 1);
      active = render;
      await render.promise;
      target.cleanup?.();
    })();
    return () => {
      cancelled = true;
      active?.cancel();
    };
  }, [page, pdf, visible]);

  useEffect(() => {
    if (active && wrapperRef.current) {
      wrapperRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [active]);

  return (
    <button
      aria-current={active ? "true" : undefined}
      aria-label={`Go to page ${page}`}
      className={`artifact-pdf-thumbnail${active ? " is-active" : ""}`}
      onClick={() => onSelect(page)}
      ref={wrapperRef}
      type="button"
    >
      <canvas className="artifact-pdf-thumbnail-canvas" ref={canvasRef} />
      <span className="artifact-pdf-thumbnail-label tabular-nums">{page}</span>
    </button>
  );
}
