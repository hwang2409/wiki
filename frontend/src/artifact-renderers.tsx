import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { ChevronDown, Copy, FileJson } from "lucide-react";
import type {
  ArtifactColumn,
  ArtifactFileEntry,
  SessionArtifact,
  SessionEvent,
} from "./api";
import { classifyArtifact } from "./artifact-kind";
import { ArtifactError, ArtifactPlaceholder } from "./artifact-state";
import { DiffPatchView } from "./diff-view";
import {
  loadPdfFromUrl,
  renderPageToCanvas,
  type LoadedPdf,
} from "./pdfjs-runtime";
import { ShikiCode, useCurrentTheme } from "./shiki";
import { StatusBadge, statusToTone } from "./status-badge";

const TABLE_ROW_HEIGHT = 32;
const TABLE_VIEWPORT_HEIGHT = 320;
const TABLE_OVERSCAN = 8;

const SVG_TAGS = [
  "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline",
  "polygon", "text", "tspan", "defs", "use", "symbol", "title", "desc",
  "style", "lineargradient", "radialgradient", "stop", "pattern",
  "clippath", "mask", "image", "marker",
];

export type ArtifactRenderFailure = {
  failureClass: "mermaid-render" | "svg-render";
  errorCode: string;
  position?: string;
};

export type ArtifactRendererProps = {
  artifact: SessionArtifact;
  compact?: boolean;
  event: SessionEvent;
  onImageLoad?: (image: HTMLImageElement) => void;
  onOpenFile?: (entry: ArtifactFileEntry) => void;
  onRenderError?: (failure: ArtifactRenderFailure) => void;
  ticket: string;
};

export function artifactUrl(ticket: string, event: SessionEvent): string {
  return `/api/agents/${encodeURIComponent(ticket)}/artifact/${encodeURIComponent(event.artifact_id ?? "")}`;
}

function normalizeRenderFailure(
  failureClass: ArtifactRenderFailure["failureClass"],
  reason: unknown,
): ArtifactRenderFailure {
  const message = reason instanceof Error ? reason.message : "";
  const errorCode = failureClass === "mermaid-render"
    ? /lexical error/i.test(message)
      ? "LEXICAL_ERROR"
      : /parse error/i.test(message)
        ? "PARSE_ERROR"
        : "MERMAID_RENDER_ERROR"
    : "SVG_RENDER_ERROR";
  const line = message.match(/\bline\s+(\d{1,5})\b/i)?.[1];
  const column = message.match(/\bcolumn\s+(\d{1,5})\b/i)?.[1];
  const position = line ? `line ${line}${column ? `, column ${column}` : ""}` : undefined;
  return { failureClass, errorCode, ...(position ? { position } : {}) };
}

function viewBoxBounds(source: string): { width: number; height: number } | null {
  const openTag = source.match(/<svg\b[^>]*>/i)?.[0] ?? "";
  const viewBox = openTag.match(/(?:^|\s)viewBox\s*=\s*["']\s*[-0-9.]+\s+[-0-9.]+\s+([0-9.]+)\s+([0-9.]+)\s*["']/i);
  if (!viewBox) return null;
  const width = Number(viewBox[1]);
  const height = Number(viewBox[2]);
  return Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0 ? { width, height } : null;
}

function numericSvgAttribute(source: string, name: string): number | null {
  const openTag = source.match(/<svg\b[^>]*>/i)?.[0] ?? "";
  const match = openTag.match(new RegExp(`(?:^|\\s)${name}\\s*=\\s*["']([0-9.]+)(?:px)?["']`, "i"));
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : null;
}

export function svgBounds(source: string): { width: number; height: number } | null {
  const width = numericSvgAttribute(source, "width");
  const height = numericSvgAttribute(source, "height");
  if (width !== null && height !== null) return { width, height };
  return viewBoxBounds(source);
}

function withExplicitSvgDimensions(source: string): string {
  const bounds = svgBounds(source);
  const openTag = source.match(/<svg\b[^>]*>/i)?.[0];
  if (!bounds || !openTag) return source;
  const dimensions = `width: ${bounds.width}px !important; height: ${bounds.height}px !important; max-width: none !important; max-height: none !important;`;
  const withoutDimensions = openTag.replace(/\s(?:width|height)\s*=\s*(?:"[^"]*"|'[^']*')/gi, "");
  const withStyle = /\sstyle=(["'])(.*?)\1/i.test(withoutDimensions)
    ? withoutDimensions.replace(/\sstyle=(["'])(.*?)\1/i, (_match, quote: string, style: string) => ` style=${quote}${style}; ${dimensions}${quote}`)
    : `${withoutDimensions.slice(0, -1)} style="${dimensions}">`;
  const sizedTag = withStyle.replace(/>$/, ` width="${bounds.width}" height="${bounds.height}">`);
  return source.replace(openTag, sizedTag);
}

export function MermaidRenderer({
  compact = false,
  onRenderError,
  source,
}: {
  compact?: boolean;
  onRenderError?: (failure: ArtifactRenderFailure) => void;
  source: string;
}) {
  const theme = useCurrentTheme();
  const reactId = useId();
  const [html, setHtml] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const id = `wiki-artifact-${reactId.replace(/[^a-zA-Z0-9_-]/g, "")}`;
    const reportFailure = (reason: unknown) => {
      if (cancelled) return;
      setHtml("");
      const message = reason instanceof Error ? reason.message : "Mermaid could not render this source.";
      setError(message);
      onRenderError?.(normalizeRenderFailure("mermaid-render", reason));
    };
    void import("mermaid").then(async ({ default: mermaid }) => {
      try {
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme.includes("light") ? "default" : "dark",
          fontFamily: getComputedStyle(document.documentElement).getPropertyValue("--font-monospace"),
        });
        const rendered = await mermaid.render(id, source);
        if (!cancelled) {
          setHtml(compact ? withExplicitSvgDimensions(rendered.svg) : rendered.svg);
          setError(null);
        }
      } catch (reason) {
        reportFailure(reason);
      }
    }).catch(reportFailure);
    return () => {
      cancelled = true;
    };
  }, [compact, nonce, onRenderError, reactId, source, theme]);

  if (error) {
    return (
      <ArtifactError
        detail={error}
        onRetry={() => {
          setError(null);
          setNonce((value) => value + 1);
        }}
        title="Diagram couldn’t render."
      />
    );
  }
  if (!html) return <ArtifactPlaceholder label="Rendering diagram…" shape="diagram" />;
  return <div className="artifact-mermaid" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function SvgRenderer({
  compact = false,
  onRenderError,
  source,
}: {
  compact?: boolean;
  onRenderError?: (failure: ArtifactRenderFailure) => void;
  source: string;
}) {
  const [html, setHtml] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const reportFailure = (reason: unknown) => {
      if (cancelled) return;
      setHtml("");
      const message = reason instanceof Error ? reason.message : "SVG could not render this source.";
      setError(message);
      onRenderError?.(normalizeRenderFailure("svg-render", reason));
    };
    void import("dompurify").then(({ default: DOMPurify }) => {
      try {
        const sanitized = DOMPurify.sanitize(source, {
          ALLOWED_TAGS: SVG_TAGS,
          FORBID_TAGS: ["script", "foreignObject", "iframe"],
        });
        if (!/<svg(?:\s|>)/i.test(sanitized)) {
          throw new Error("SVG sanitizer produced no <svg> output.");
        }
        if (!cancelled) {
          setHtml(compact ? withExplicitSvgDimensions(sanitized) : sanitized);
          setError(null);
        }
      } catch (reason) {
        reportFailure(reason);
      }
    }).catch(reportFailure);
    return () => {
      cancelled = true;
    };
  }, [compact, nonce, onRenderError, source]);
  if (error) {
    return (
      <ArtifactError
        detail={error}
        onRetry={() => {
          setError(null);
          setNonce((value) => value + 1);
        }}
        title="Image couldn’t render."
      />
    );
  }
  if (!html) return <ArtifactPlaceholder label="Preparing image…" shape="image" />;
  return <div className="artifact-svg" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function SharedImageRenderer({
  alt,
  imgClassName,
  onImageLoad,
  source,
  style,
  wrapClassName,
}: {
  alt: string;
  imgClassName?: string;
  onImageLoad?: (image: HTMLImageElement) => void;
  source: string;
  style?: CSSProperties;
  wrapClassName?: string;
}) {
  const [state, setState] = useState<"loading" | "ready" | "error">("loading");
  const [nonce, setNonce] = useState(0);
  useEffect(() => setState("loading"), [source, nonce]);
  if (state === "error") {
    return (
      <div className={`artifact-image-wrap${wrapClassName ? ` ${wrapClassName}` : ""}`}>
        <ArtifactError
          detail={`Failed to load ${source.startsWith("data:") ? "inline image data" : source}`}
          onRetry={() => setNonce((value) => value + 1)}
          title="Image couldn’t load."
        />
      </div>
    );
  }
  return (
    <div className={`artifact-image-wrap${wrapClassName ? ` ${wrapClassName}` : ""}`}>
      {state === "loading" ? <ArtifactPlaceholder label="Loading image…" shape="image" /> : null}
      <img
        key={nonce}
        alt={alt}
        className={imgClassName}
        loading="lazy"
        src={source}
        style={state === "loading" ? { visibility: "hidden", position: "absolute", inset: 0, ...style } : style}
        onError={() => setState("error")}
        onLoad={(loadEvent) => {
          setState("ready");
          onImageLoad?.(loadEvent.currentTarget);
        }}
      />
    </div>
  );
}

export function ImageRenderer({ artifact, event, onImageLoad, ticket }: ArtifactRendererProps) {
  const source = artifact.data_base64
    ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
    : artifactUrl(ticket, event);
  return (
    <SharedImageRenderer
      alt={event.title || event.caption || "Agent artifact"}
      imgClassName="artifact-image"
      onImageLoad={onImageLoad}
      source={source}
    />
  );
}

export function PlotRenderer({ actions = false, spec }: { actions?: boolean; spec: Record<string, unknown> }) {
  const theme = useCurrentTheme();
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    setReady(false);
    const target = container.current;
    if (!target) return;
    let finalized = false;
    let finalize: (() => void) | undefined;
    const styles = getComputedStyle(document.documentElement);
    const text = styles.getPropertyValue("--text-normal").trim();
    const muted = styles.getPropertyValue("--text-muted").trim();
    const border = styles.getPropertyValue("--background-modifier-border").trim();
    const background = styles.getPropertyValue("--background-primary").trim();
    const accent = styles.getPropertyValue("--accent-primary").trim();
    const font = styles.getPropertyValue("--font-monospace").trim();
    const sourceConfig: Record<string, unknown> =
      typeof spec.config === "object" && spec.config ? spec.config as Record<string, unknown> : {};
    const sourceAxis = typeof sourceConfig.axis === "object" && sourceConfig.axis ? sourceConfig.axis : {};
    const sourceLegend = typeof sourceConfig.legend === "object" && sourceConfig.legend ? sourceConfig.legend : {};
    const sourceTitle = typeof sourceConfig.title === "object" && sourceConfig.title ? sourceConfig.title : {};
    const sourceRange = typeof sourceConfig.range === "object" && sourceConfig.range ? sourceConfig.range : {};
    const themedSpec = {
      ...spec,
      background,
      config: {
        ...sourceConfig,
        font,
        background,
        axis: { ...sourceAxis, domainColor: border, gridColor: border, labelColor: muted, titleColor: text },
        legend: { ...sourceLegend, labelColor: muted, titleColor: text },
        title: { ...sourceTitle, color: text, font },
        range: { ...sourceRange, category: [accent, text, muted, border] },
      },
    };
    const reportPlotFailure = (reason: unknown) => {
      if (finalized) return;
      setError(reason instanceof Error ? reason.message : "Plot could not render this spec.");
    };
    import("vega-embed").then(async ({ default: embed }) => {
      try {
        const result = await embed(target, themedSpec, { actions, renderer: "svg" });
        finalize = result.finalize;
        if (!finalized) {
          setError(null);
          setReady(true);
        }
      } catch (reason) {
        reportPlotFailure(reason);
      }
    }).catch(reportPlotFailure);
    return () => {
      finalized = true;
      finalize?.();
      target.replaceChildren();
    };
  }, [actions, nonce, spec, theme]);
  if (error) {
    return (
      <ArtifactError
        detail={error}
        onRetry={() => {
          setError(null);
          setNonce((value) => value + 1);
        }}
        title="Plot couldn’t render."
      />
    );
  }
  return (
    <div className="artifact-plot-wrap">
      {!ready ? <ArtifactPlaceholder label="Rendering plot…" shape="plot" /> : null}
      <div className="artifact-plot" ref={container} style={ready ? undefined : { visibility: "hidden", position: "absolute" }} />
    </div>
  );
}

function csvCell(value: unknown): string {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function tableText(
  artifact: SessionArtifact,
  format: "tsv" | "csv" | "json"
): string {
  const columns = artifact.columns ?? [];
  const rows = artifact.rows ?? [];
  if (format === "json") {
    return JSON.stringify(
      rows.map((row) => Object.fromEntries(columns.map((column, index) => [column.key, row[index] ?? null]))),
      null,
      2
    );
  }
  const separator = format === "tsv" ? "\t" : ",";
  const encode = format === "tsv" ? (value: unknown) => String(value ?? "") : csvCell;
  return [
    columns.map((column) => encode(column.label)).join(separator),
    ...rows.map((row) => row.map(encode).join(separator)),
  ].join("\n");
}

function compareCells(left: unknown, right: unknown, column: ArtifactColumn): number {
  if (column.type === "number") return Number(left ?? 0) - Number(right ?? 0);
  if (column.type === "date") {
    return new Date(String(left ?? "")).getTime() - new Date(String(right ?? "")).getTime();
  }
  return String(left ?? "").localeCompare(String(right ?? ""), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

function safeTableLink(value: string): string | null {
  try {
    const url = new URL(value, window.location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? value : null;
  } catch {
    return null;
  }
}

function TableRenderer({ artifact }: { artifact: SessionArtifact }) {
  const columns = artifact.columns ?? [];
  const rows = artifact.rows ?? [];
  const [sort, setSort] = useState<{ index: number; direction: 1 | -1 } | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    const column = columns[sort.index];
    if (!column) return rows;
    return rows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const compared = compareCells(left.row[sort.index], right.row[sort.index], column);
        return compared === 0 ? left.index - right.index : compared * sort.direction;
      })
      .map(({ row }) => row);
  }, [columns, rows, sort]);
  const start = Math.max(0, Math.floor(scrollTop / TABLE_ROW_HEIGHT) - TABLE_OVERSCAN);
  const visibleCount = Math.ceil(TABLE_VIEWPORT_HEIGHT / TABLE_ROW_HEIGHT) + TABLE_OVERSCAN * 2;
  const end = Math.min(sortedRows.length, start + visibleCount);
  const visible = sortedRows.slice(start, end);

  return (
    <div
      className="artifact-table-scroll"
      onScroll={(event) => setScrollTop(event.currentTarget.scrollTop)}
      style={{ maxHeight: TABLE_VIEWPORT_HEIGHT }}
    >
      <table className="artifact-table tabular-nums">
        <thead>
          <tr>
            {columns.map((column, index) => {
              const active = sort?.index === index;
              return (
                <th className={`is-${column.type}`} key={column.key}>
                  <button
                    aria-label={`Sort by ${column.label}`}
                    type="button"
                    onClick={() =>
                      setSort((current) => ({
                        index,
                        direction: current?.index === index && current.direction === 1 ? -1 : 1,
                      }))
                    }
                  >
                    {column.label}
                    {active ? <span aria-hidden="true">{sort.direction === 1 ? "↑" : "↓"}</span> : null}
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {start > 0 ? (
            <tr aria-hidden="true" className="artifact-table-spacer">
              <td colSpan={columns.length} style={{ height: start * TABLE_ROW_HEIGHT }} />
            </tr>
          ) : null}
          {visible.map((row, rowOffset) => (
            <tr key={start + rowOffset}>
              {columns.map((column, columnIndex) => {
                const value = row[columnIndex];
                const link = column.type === "link" && typeof value === "string" ? safeTableLink(value) : null;
                return (
                  <td className={`is-${column.type}`} key={column.key}>
                    {link ? (
                      <a href={link} rel="noreferrer" target="_blank">{value}</a>
                    ) : column.type === "date" && value ? (
                      <time dateTime={String(value)}>{new Date(String(value)).toLocaleString()}</time>
                    ) : (
                      String(value ?? "")
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
          {end < sortedRows.length ? (
            <tr aria-hidden="true" className="artifact-table-spacer">
              <td colSpan={columns.length} style={{ height: (sortedRows.length - end) * TABLE_ROW_HEIGHT }} />
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

function CodeRenderer({ artifact }: { artifact: SessionArtifact }) {
  const [code, setCode] = useState(artifact.source ?? "");
  const isDiff = artifact.diff_from !== undefined;
  useEffect(() => {
    if (!isDiff) {
      setCode(artifact.source ?? "");
      return;
    }
    let cancelled = false;
    void import("diff").then(({ createTwoFilesPatch }) => {
      const filename = artifact.filename || "artifact";
      const patch = createTwoFilesPatch(
        filename,
        filename,
        artifact.diff_from ?? "",
        artifact.source ?? "",
        "before",
        "after",
        { context: 3 }
      );
      if (!cancelled) setCode(patch);
    });
    return () => {
      cancelled = true;
    };
  }, [artifact.diff_from, artifact.filename, artifact.source, isDiff]);
  return (
    <div className="artifact-code" data-diff={isDiff || undefined}>
      {artifact.filename ? <div className="artifact-code-filename">{artifact.filename}</div> : null}
      <ShikiCode code={code} lang={isDiff ? "diff" : artifact.language} />
    </div>
  );
}

function DiffRenderer({ artifact }: { artifact: SessionArtifact }) {
  return (
    <div className="artifact-diff">
      <DiffPatchView source={artifact.source ?? ""} />
    </div>
  );
}

function FileListRenderer({
  artifact,
  onOpenFile,
}: {
  artifact: SessionArtifact;
  onOpenFile?: (entry: ArtifactFileEntry) => void;
}) {
  const files = artifact.files ?? [];
  if (files.length === 0) {
    return <div className="artifact-file-list-empty">No files.</div>;
  }
  return (
    <ul className="artifact-file-list">
      {files.map((entry, index) => {
        const label = entry.label ?? entry.path;
        const clickable = Boolean(onOpenFile);
        return (
          <li className="artifact-file-list-item" key={`${entry.path}-${index}`}>
            {clickable ? (
              <button
                className="artifact-file-list-button"
                onClick={() => onOpenFile?.(entry)}
                title={entry.path}
                type="button"
              >
                <FileJson aria-hidden="true" className="artifact-file-list-icon" size={12} />
                <span className="artifact-file-list-label">{label}</span>
                {entry.status ? (
                  <StatusBadge
                    className="artifact-file-list-status"
                    compact
                    label={entry.status}
                    state={statusToTone(entry.status)}
                  />
                ) : null}
              </button>
            ) : (
              <span className="artifact-file-list-static" title={entry.path}>
                <FileJson aria-hidden="true" className="artifact-file-list-icon" size={12} />
                <span className="artifact-file-list-label">{label}</span>
                {entry.status ? (
                  <StatusBadge
                    className="artifact-file-list-status"
                    compact
                    label={entry.status}
                    state={statusToTone(entry.status)}
                  />
                ) : null}
              </span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function JsonRenderer({ artifact }: { artifact: SessionArtifact }) {
  const value = artifact.json_data !== undefined ? artifact.json_data : artifact.source;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <pre className="artifact-json">
      <code>{text}</code>
    </pre>
  );
}

export function TableCopyMenu({ artifact, onCopied }: { artifact: SessionArtifact; onCopied: () => void }) {
  const [open, setOpen] = useState(false);
  async function copy(format: "tsv" | "csv" | "json") {
    await navigator.clipboard.writeText(tableText(artifact, format));
    setOpen(false);
    onCopied();
  }
  return (
    <div className="artifact-copy-menu">
      <button aria-expanded={open} className="artifact-action" type="button" onClick={() => setOpen((value) => !value)}>
        <Copy size={12} /> Copy <ChevronDown size={11} />
      </button>
      {open ? (
        <div className="artifact-copy-options" role="menu">
          <button type="button" role="menuitem" onClick={() => void copy("tsv")}>Copy as TSV</button>
          <button type="button" role="menuitem" onClick={() => void copy("csv")}>Copy as CSV</button>
          <button type="button" role="menuitem" onClick={() => void copy("json")}>Copy as JSON</button>
        </div>
      ) : null}
    </div>
  );
}

const PDF_INLINE_WIDTH = 320;

type PdfLoadState =
  | { status: "loading" }
  | { status: "ready"; pdf: LoadedPdf; aspect: number }
  | { status: "error"; message: string };

export function usePdfDocument(url: string): {
  state: PdfLoadState;
  reload: () => void;
} {
  const [state, setState] = useState<PdfLoadState>({ status: "loading" });
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    let current: LoadedPdf | null = null;
    setState({ status: "loading" });
    (async () => {
      try {
        const pdf = await loadPdfFromUrl(url);
        if (cancelled) {
          await pdf.destroy();
          return;
        }
        current = pdf;
        const first = await pdf.doc.getPage(1);
        let aspect: number;
        try {
          const viewport = first.getViewport({ scale: 1 });
          aspect = viewport.height / viewport.width;
        } finally {
          first.cleanup?.();
        }
        if (cancelled) return;
        setState({ status: "ready", pdf, aspect });
      } catch (error) {
        if (cancelled) return;
        setState({
          status: "error",
          message: error instanceof Error ? error.message : "Failed to load PDF.",
        });
      }
    })();
    return () => {
      cancelled = true;
      current?.destroy().catch(() => {});
    };
  }, [nonce, url]);
  return { state, reload: () => setNonce((value) => value + 1) };
}

export function PdfCompactRenderer({
  event,
  ticket,
}: {
  event: SessionEvent;
  ticket: string;
}) {
  const url = artifactUrl(ticket, event);
  const { state, reload } = usePdfDocument(url);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    if (state.status !== "ready") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;
    let active: { cancel: () => void } | null = null;
    (async () => {
      let page: import("pdfjs-dist").PDFPageProxy | null = null;
      try {
        page = await state.pdf.doc.getPage(1);
        const baseViewport = page.getViewport({ scale: 1 });
        const scale = PDF_INLINE_WIDTH / baseViewport.width;
        if (cancelled) return;
        const render = renderPageToCanvas(page, canvas, scale, window.devicePixelRatio || 1);
        active = render;
        await render.promise;
      } catch {
        // Retry surfaces the error via reload button.
      } finally {
        // Release the compact-preview page proxy in every exit path so
        // pdf.js doesn't retain a page-1 handle per compact renderer.
        page?.cleanup?.();
      }
    })();
    return () => {
      cancelled = true;
      active?.cancel();
    };
  }, [state]);

  if (state.status === "error") {
    return (
      <ArtifactError
        detail={state.message}
        onRetry={reload}
        title="PDF failed to load."
      />
    );
  }
  const aspect = state.status === "ready" ? state.aspect : 1.294; // ~US Letter default
  const height = Math.round(PDF_INLINE_WIDTH * aspect);
  return (
    <div className="artifact-pdf-compact" style={{ width: PDF_INLINE_WIDTH }}>
      <div
        className="artifact-pdf-compact-frame"
        style={{ width: PDF_INLINE_WIDTH, height }}
      >
        {state.status === "loading" ? (
          <ArtifactPlaceholder label="Loading PDF…" shape="image" />
        ) : (
          <canvas
            aria-label={event.title || event.caption || "PDF first page thumbnail"}
            className="artifact-pdf-compact-canvas"
            ref={canvasRef}
          />
        )}
      </div>
      {state.status === "ready" ? (
        <div className="artifact-pdf-compact-meta tabular-nums">
          page 1 of {state.pdf.numPages}
        </div>
      ) : null}
    </div>
  );
}

export function ArtifactRenderer(props: ArtifactRendererProps): ReactNode {
  const { artifact } = props;
  const effectiveKind = classifyArtifact(artifact);
  switch (effectiveKind) {
    case "mermaid":
      return <MermaidRenderer compact={props.compact} onRenderError={props.onRenderError} source={artifact.source ?? ""} />;
    case "svg":
      return <SvgRenderer compact={props.compact} onRenderError={props.onRenderError} source={artifact.source ?? ""} />;
    case "image":
      return <ImageRenderer {...props} />;
    case "table":
      return <TableRenderer artifact={artifact} />;
    case "plot":
      return <PlotRenderer spec={artifact.spec_vega_lite ?? {}} />;
    case "diff":
      return <DiffRenderer artifact={artifact} />;
    case "file-list":
      return <FileListRenderer artifact={artifact} onOpenFile={props.onOpenFile} />;
    case "json":
      return <JsonRenderer artifact={artifact} />;
    case "code":
      return <CodeRenderer artifact={artifact} />;
    case "pdf":
      return <PdfCompactRenderer event={props.event} ticket={props.ticket} />;
  }
}

export function CompactPreview({ artifact, event, onRenderError, ticket }: ArtifactRendererProps) {
  const effectiveKind = classifyArtifact(artifact);
  if (effectiveKind === "table") {
    const columns = artifact.columns ?? [];
    const rows = artifact.rows ?? [];
    return (
      <div className="artifact-compact-table">
        <table className="artifact-table tabular-nums">
          <thead><tr>{columns.map((column) => <th className={`is-${column.type}`} key={column.key}><span>{column.label}</span></th>)}</tr></thead>
          <tbody>{rows.slice(0, 5).map((row, rowIndex) => <tr key={rowIndex}>{columns.map((column, columnIndex) => <td className={`is-${column.type}`} key={column.key}>{String(row[columnIndex] ?? "")}</td>)}</tr>)}</tbody>
        </table>
        <span className="artifact-compact-summary">+ {Math.max(0, rows.length - 5)} rows (click to open)</span>
      </div>
    );
  }
  if (effectiveKind === "diff") {
    const lines = (artifact.source ?? "").split("\n");
    const previewSource = lines.slice(0, 40).join("\n");
    return (
      <div className="artifact-compact-diff">
        <div className="artifact-diff">
          <DiffPatchView source={previewSource} />
        </div>
        <span className="artifact-compact-summary">… {Math.max(0, lines.length - 40)} lines folded · Open in panel</span>
      </div>
    );
  }
  if (effectiveKind === "code") {
    const lines = (artifact.source ?? "").split("\n");
    return (
      <div className="artifact-compact-code">
        {artifact.filename ? <div className="artifact-code-filename">{artifact.filename}</div> : null}
        <pre><code>{lines.slice(0, 10).join("\n")}</code></pre>
        <span className="artifact-compact-summary">… {lines.length - 10} lines folded · Open in panel</span>
      </div>
    );
  }
  if (effectiveKind === "image") {
    const source = artifact.data_base64
      ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
      : artifactUrl(ticket, event);
    return (
      <SharedImageRenderer
        alt={event.title || event.caption || "Agent artifact"}
        imgClassName="artifact-image artifact-compact-image"
        source={source}
        wrapClassName="artifact-image-compact-wrap"
      />
    );
  }
  if (effectiveKind === "mermaid" || effectiveKind === "svg") {
    return (
      <div className="artifact-compact-diagram">
        <ArtifactRenderer artifact={artifact} compact event={event} onRenderError={onRenderError} ticket={ticket} />
        <span className="artifact-compact-diagram-hint">Diagram continues · Click to inspect</span>
      </div>
    );
  }
  if (effectiveKind === "pdf") {
    return <PdfCompactRenderer event={event} ticket={ticket} />;
  }
  return <ArtifactRenderer artifact={artifact} event={event} ticket={ticket} />;
}
