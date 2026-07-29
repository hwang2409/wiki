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

const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2] as const;

type ZoomMode = "fit-width" | "fit-page" | number;

function resolveZoom(mode: ZoomMode, viewport: { width: number; height: number }, page: { width: number; height: number }): number {
  if (mode === "fit-width") return Math.max(0.1, (viewport.width - 48) / page.width);
  if (mode === "fit-page") {
    return Math.max(
      0.1,
      Math.min((viewport.width - 48) / page.width, (viewport.height - 48) / page.height),
    );
  }
  return mode;
}

function nextZoomStep(current: number, direction: 1 | -1): number {
  const steps = ZOOM_STEPS as readonly number[];
  if (direction === 1) {
    const found = steps.find((step) => step > current + 0.0001);
    return found ?? steps[steps.length - 1];
  }
  const found = [...steps].reverse().find((step) => step < current - 0.0001);
  return found ?? steps[0];
}

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
  const [textIndexPageCount, setTextIndexPageCount] = useState(0);
  const [currentMatch, setCurrentMatch] = useState<number>(0);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const [pageBaseSize, setPageBaseSize] = useState<{ width: number; height: number } | null>(null);
  const numPages = loadState.status === "ready" ? loadState.pdf.numPages : 0;

  useEffect(() => {
    setPage(1);
    setTextIndex([]);
    setTextIndexPageCount(0);
    setPageBaseSize(null);
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
    })();
    return () => {
      cancelled = true;
      active?.cancel();
    };
  }, [loadState, page, zoom, pageBaseSize]);

  useEffect(() => {
    if (loadState.status !== "ready" || textIndexPageCount === loadState.pdf.numPages) return;
    let cancelled = false;
    (async () => {
      const accumulator: PageTextIndex[] = [];
      for (let index = 1; index <= loadState.pdf.numPages; index += 1) {
        if (cancelled) return;
        const target = await loadState.pdf.doc.getPage(index);
        const text = await extractPageText(target);
        accumulator.push({ page: index, text });
      }
      if (cancelled) return;
      setTextIndex(accumulator);
      setTextIndexPageCount(loadState.pdf.numPages);
    })();
    return () => {
      cancelled = true;
    };
  }, [loadState, textIndexPageCount]);

  const matches = useMemo<FindMatch[]>(
    () => (findValue ? findMatches(textIndex, findValue) : []),
    [findValue, textIndex],
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
      const command = nativeEvent.metaKey || nativeEvent.ctrlKey;
      if (command && nativeEvent.key === "ArrowLeft") {
        nativeEvent.preventDefault();
        goPrev();
        return;
      }
      if (command && nativeEvent.key === "ArrowRight") {
        nativeEvent.preventDefault();
        goNext();
        return;
      }
      if (command && nativeEvent.key.toLowerCase() === "f") {
        nativeEvent.preventDefault();
        setFindOpen(true);
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [goNext, goPrev]);

  function update(next: Partial<ArtifactViewState>) {
    onChange({ ...state, ...next });
  }

  function setZoomStep(delta: 1 | -1) {
    setZoomMode(nextZoomStep(zoom, delta));
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
          <button aria-label="Previous page" data-pdf-prev="true" type="button" onClick={goPrev} disabled={page <= 1}>
            <ChevronLeft size={12} />
          </button>
          <span className="artifact-pdf-page-indicator tabular-nums">
            page {page} of {numPages || "…"}
          </span>
          <button aria-label="Next page" data-pdf-next="true" type="button" onClick={goNext} disabled={!numPages || page >= numPages}>
            <ChevronRight size={12} />
          </button>
        </div>
        <div className="artifact-pdf-toolbar-group">
          <button aria-label="Fit width" type="button" onClick={() => setZoomMode("fit-width")} data-pdf-fit-width="true">
            fit width
          </button>
          <button aria-label="Fit page" type="button" onClick={() => setZoomMode("fit-page")} data-pdf-fit-page="true">
            <Maximize2 size={12} /> fit page
          </button>
          <button aria-label="Zoom out" type="button" onClick={() => setZoomStep(-1)} data-pdf-zoom-out="true">
            <Minus size={12} />
          </button>
          <span className="artifact-pdf-zoom-value tabular-nums">{percent}%</span>
          <button aria-label="Zoom in" type="button" onClick={() => setZoomStep(1)} data-pdf-zoom-in="true">
            <Plus size={12} />
          </button>
          <button data-panel-reset-zoom="true" type="button" onClick={() => setZoomMode(1)}>
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
              <span className="tabular-nums">
                {totalMatches ? `${activeMatch}/${totalMatches}` : "0 matches"}
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
                aria-hidden="true"
                className="artifact-pdf-page-textlayer"
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
  if (!pdf || !numPages) {
    return <aside className="artifact-pdf-thumbnails" aria-label="PDF thumbnails" />;
  }
  return (
    <aside className="artifact-pdf-thumbnails" aria-label="PDF thumbnails">
      {Array.from({ length: numPages }, (_, index) => index + 1).map((pageNumber) => (
        <PdfThumbnail
          active={pageNumber === activePage}
          key={pageNumber}
          onSelect={onSelect}
          page={pageNumber}
          pdf={pdf}
        />
      ))}
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
  const wrapperRef = useRef<HTMLButtonElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const target = wrapperRef.current;
    if (!target) return;
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) setVisible(true);
        });
      },
      { rootMargin: "200px" },
    );
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

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
