import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  BarChart3,
  ChevronDown,
  Code2,
  Copy,
  Download,
  FileJson,
  GitBranch,
  Image as ImageIcon,
  Info,
  PanelRightOpen,
  Shapes,
  Table2,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { sendAgentMessage } from "./api";
import type {
  ArtifactColumn,
  ArtifactFileEntry,
  ArtifactKind,
  SessionArtifact,
  SessionEvent,
} from "./api";
import { classifyArtifact } from "./artifact-kind";
import { DiffPatchView } from "./diff-view";
import { ShikiCode, useCurrentTheme } from "./shiki";
import { SplitDiffView } from "./split-diff";
import { StatusBadge, statusToTone } from "./status-badge";

const TABLE_ROW_HEIGHT = 32;
const TABLE_VIEWPORT_HEIGHT = 320;
const TABLE_OVERSCAN = 8;
const SVG_TAGS = [
  "svg",
  "g",
  "path",
  "rect",
  "circle",
  "ellipse",
  "line",
  "polyline",
  "polygon",
  "text",
  "tspan",
  "defs",
  "use",
  "symbol",
  "title",
  "desc",
  "style",
  "lineargradient",
  "radialgradient",
  "stop",
  "pattern",
  "clippath",
  "mask",
  "image",
  "marker",
];

const KIND_ICONS: Record<ArtifactKind, LucideIcon> = {
  mermaid: GitBranch,
  svg: Shapes,
  image: ImageIcon,
  table: Table2,
  plot: BarChart3,
  code: Code2,
  diff: GitBranch,
  "file-list": FileJson,
  json: FileJson,
};

export function artifactUrl(ticket: string, event: SessionEvent): string {
  return `/api/agents/${encodeURIComponent(ticket)}/artifact/${encodeURIComponent(event.artifact_id ?? "")}`;
}

function csvCell(value: unknown): string {
  const text = value == null ? "" : String(value);
  return /[",\n\r]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function tableText(
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

function textPayload(artifact: SessionArtifact): string {
  switch (artifact.kind) {
    case "mermaid":
    case "svg":
    case "code":
    case "diff":
      return artifact.source ?? "";
    case "table":
      return tableText(artifact, "tsv");
    case "plot":
      return JSON.stringify(artifact.spec_vega_lite ?? {}, null, 2);
    case "image":
      return artifact.data_base64 ?? artifact.ref ?? "";
    case "file-list":
      return (artifact.files ?? []).map((entry) => entry.path).join("\n");
    case "json":
      return typeof artifact.json_data === "string"
        ? artifact.json_data
        : JSON.stringify(artifact.json_data ?? {}, null, 2);
  }
}

async function imageBase64(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Image download failed (${response.status})`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

function downloadName(event: SessionEvent): string {
  const artifact = event.artifact!;
  const effectiveKind = classifyArtifact(artifact);
  const base = (event.title || `artifact-${event.artifact_id?.slice(0, 8) || effectiveKind}`)
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "artifact";
  if (effectiveKind === "code" && artifact.filename) {
    return artifact.filename.split(/[\\/]/).pop() || `${base}.txt`;
  }
  const extension = {
    mermaid: "mmd",
    svg: "svg",
    image: artifact.mime === "image/jpeg" ? "jpg" : artifact.mime?.split("/")[1] || "png",
    table: "csv",
    plot: "json",
    code: artifact.language?.replace(/[^a-zA-Z0-9]/g, "") || "txt",
    diff: "diff",
    "file-list": "txt",
    json: "json",
  }[effectiveKind];
  return `${base}.${extension}`;
}

type ArtifactRenderFailure = {
  failureClass: "mermaid-render" | "svg-render";
  errorCode: string;
  position?: string;
};

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
  }, [compact, onRenderError, reactId, source, theme]);

  if (error) return <div className="artifact-error">{error}</div>;
  if (!html) return <div className="artifact-loading">Rendering diagram…</div>;
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
  }, [compact, onRenderError, source]);
  if (error) return <div className="artifact-error">{error}</div>;
  if (!html) return <div className="artifact-loading">Sanitizing SVG…</div>;
  return <div className="artifact-svg" dangerouslySetInnerHTML={{ __html: html }} />;
}

export function ImageRenderer({ artifact, event, onImageLoad, ticket }: ArtifactRendererProps) {
  const source = artifact.data_base64
    ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
    : artifactUrl(ticket, event);
  return (
    <img
      alt={event.title || event.caption || "Agent artifact"}
      className="artifact-image"
      loading="lazy"
      src={source}
      onLoad={(loadEvent) => onImageLoad?.(loadEvent.currentTarget)}
    />
  );
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

export function PlotRenderer({ actions = false, spec }: { actions?: boolean; spec: Record<string, unknown> }) {
  const theme = useCurrentTheme();
  const container = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
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
    void import("vega-embed").then(async ({ default: embed }) => {
      try {
        const result = await embed(target, themedSpec, { actions, renderer: "svg" });
        finalize = result.finalize;
        if (!finalized) setError(null);
      } catch (reason) {
        if (!finalized) setError(reason instanceof Error ? reason.message : "Plot could not render this spec.");
      }
    });
    return () => {
      finalized = true;
      finalize?.();
      target.replaceChildren();
    };
  }, [actions, spec, theme]);
  return error ? <div className="artifact-error">{error}</div> : <div className="artifact-plot" ref={container} />;
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

export type ArtifactRendererProps = {
  artifact: SessionArtifact;
  compact?: boolean;
  event: SessionEvent;
  onImageLoad?: (image: HTMLImageElement) => void;
  onOpenFile?: (entry: ArtifactFileEntry) => void;
  onRenderError?: (failure: ArtifactRenderFailure) => void;
  ticket: string;
};

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
  }
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

function TableCopyMenu({ artifact, onCopied }: { artifact: SessionArtifact; onCopied: () => void }) {
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

function numericSvgAttribute(source: string, name: string): number | null {
  const openTag = source.match(/<svg\b[^>]*>/i)?.[0] ?? "";
  const match = openTag.match(new RegExp(`(?:^|\\s)${name}\\s*=\\s*["']([0-9.]+)(?:px)?["']`, "i"));
  if (!match) return null;
  const value = Number(match[1]);
  return Number.isFinite(value) ? value : null;
}

function svgBounds(source: string): { width: number; height: number } | null {
  const width = numericSvgAttribute(source, "width");
  const height = numericSvgAttribute(source, "height");
  if (width !== null && height !== null) return { width, height };
  return viewBoxBounds(source);
}

function mermaidNodeCount(source: string): number {
  const nodes = new Set<string>();
  const add = (value: string | undefined) => {
    if (value && !["flowchart", "graph", "subgraph", "end", "direction", "participant"].includes(value)) nodes.add(value);
  };
  for (const line of source.split("\n")) {
    const clean = line.replace(/%%.*$/, "");
    for (const match of clean.matchAll(/(?:^|[\s;&])([A-Za-z_][\w-]*)\s*(?=(?:\[|\(|\{|>|-->|---|-.->|==>|->>|-->>))/g)) add(match[1]);
    for (const match of clean.matchAll(/(?:-->|---|-.->|==>|->>|-->>)\s*([A-Za-z_][\w-]*)/g)) add(match[1]);
    for (const match of clean.matchAll(/\bparticipant\s+([A-Za-z_][\w-]*)/g)) add(match[1]);
  }
  return nodes.size;
}

function plotExceedsInlineBounds(spec: Record<string, unknown>) {
  const bytes = new TextEncoder().encode(JSON.stringify(spec)).byteLength;
  const width = typeof spec.width === "number" ? spec.width : 0;
  const height = typeof spec.height === "number" ? spec.height : 0;
  return bytes > 100 * 1024 || width > 640 || height > 400;
}

export function artifactExceedsInlineThreshold(
  artifact: SessionArtifact,
  imageBounds?: { width: number; height: number } | null,
): boolean {
  switch (classifyArtifact(artifact)) {
    case "table": return (artifact.rows?.length ?? 0) > 30;
    case "code": return (artifact.source ?? "").split("\n").length > 100;
    case "image": return Boolean(imageBounds && (imageBounds.width > 400 || imageBounds.height > 400));
    case "mermaid": return mermaidNodeCount(artifact.source ?? "") > 20;
    case "svg": {
      const bounds = svgBounds(artifact.source ?? "");
      return Boolean(bounds && (bounds.width > 400 || bounds.height > 400));
    }
    case "plot": return plotExceedsInlineBounds(artifact.spec_vega_lite ?? {});
    case "diff": return (artifact.source ?? "").split("\n").length > 100;
    case "file-list": return (artifact.files ?? []).length > 30;
    case "json": {
      const text = typeof artifact.json_data === "string"
        ? artifact.json_data
        : JSON.stringify(artifact.json_data ?? "");
      return text.length > 4000;
    }
  }
}

function CompactPreview({ artifact, event, onRenderError, ticket }: ArtifactRendererProps) {
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
    return <img alt={event.title || event.caption || "Agent artifact"} className="artifact-image artifact-compact-image" loading="lazy" src={source} />;
  }
  if (effectiveKind === "mermaid" || effectiveKind === "svg") {
    return (
      <div className="artifact-compact-diagram">
        <ArtifactRenderer artifact={artifact} compact event={event} onRenderError={onRenderError} ticket={ticket} />
        <span className="artifact-compact-diagram-hint">Diagram continues · Click to inspect</span>
      </div>
    );
  }
  return <ArtifactRenderer artifact={artifact} event={event} ticket={ticket} />;
}

export function ArtifactBlock({
  event,
  onOpen,
  ticket,
}: {
  event: SessionEvent;
  onOpen?: (event: SessionEvent) => void;
  ticket: string;
}) {
  const artifact = event.artifact;
  const [inspect, setInspect] = useState(false);
  const [copied, setCopied] = useState(false);
  const [imageBounds, setImageBounds] = useState<{ width: number; height: number } | null>(null);
  const [renderFailure, setRenderFailure] = useState<ArtifactRenderFailure | null>(null);
  const [deliveryStatus, setDeliveryStatus] = useState<"pending" | "queued" | "deduplicated" | "sent" | "failed" | null>(null);
  const copiedTimer = useRef<number | null>(null);
  const reportedRenderFailure = useRef<string | null>(null);
  const renderFailureArtifactId = useRef(event.artifact_id);
  useEffect(() => setImageBounds(null), [event.artifact_id]);
  useEffect(() => {
    if (renderFailureArtifactId.current === event.artifact_id) return;
    renderFailureArtifactId.current = event.artifact_id;
    setRenderFailure(null);
    setDeliveryStatus(null);
    reportedRenderFailure.current = null;
  }, [event.artifact_id]);
  useEffect(() => () => {
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
  }, []);
  const reportRenderFailure = useCallback((failure: ArtifactRenderFailure) => {
    setRenderFailure(failure);
    const artifactId = event.artifact_id ?? "unknown";
    const dedupeKey = `artifact-render:${artifactId}:${failure.failureClass}`;
    if (reportedRenderFailure.current === dedupeKey) return;
    reportedRenderFailure.current = dedupeKey;
    setDeliveryStatus("pending");
    const diagnostic = [
      "Wiki.app artifact render failure",
      "The following fields are normalized, untrusted renderer metadata; artifact source text is omitted.",
      `artifact_id: ${artifactId}`,
      `kind: ${artifact?.kind ?? "unknown"}`,
      `failure_class: ${failure.failureClass}`,
      `error_code: ${failure.errorCode}`,
      ...(failure.position ? [`position: ${failure.position}`] : []),
      "The artifact was accepted by render_artifact but the session view could not render it. Correct the source and render it again.",
    ].join("\n");
    void sendAgentMessage(ticket, diagnostic, "on-idle", undefined, dedupeKey)
      .then((response) => {
        switch (response.status) {
          case "queued":
            setDeliveryStatus("queued");
            break;
          case "deduplicated":
            setDeliveryStatus("deduplicated");
            break;
          case "sent":
            setDeliveryStatus("sent");
            break;
          default:
            setDeliveryStatus("failed");
        }
      })
      .catch(() => {
        setDeliveryStatus("failed");
      });
  }, [artifact?.kind, event.artifact_id, ticket]);
  if (!artifact) return <div className="artifact-error">Artifact payload missing.</div>;
  const resolvedArtifact = artifact;
  const Icon = KIND_ICONS[resolvedArtifact.kind] ?? FileJson;
  const rowCount = resolvedArtifact.kind === "table" ? resolvedArtifact.rows?.length ?? 0 : null;
  const oversized = artifactExceedsInlineThreshold(resolvedArtifact, imageBounds);

  function showCopied() {
    setCopied(true);
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
    copiedTimer.current = window.setTimeout(() => setCopied(false), 1400);
  }

  async function copy() {
    const value =
      resolvedArtifact.kind === "image" && !resolvedArtifact.data_base64
        ? await imageBase64(artifactUrl(ticket, event))
        : textPayload(resolvedArtifact);
    await navigator.clipboard.writeText(value);
    showCopied();
  }

  async function download() {
    let blob: Blob;
    if (resolvedArtifact.kind === "image" && !resolvedArtifact.data_base64) {
      const response = await fetch(artifactUrl(ticket, event));
      if (!response.ok) throw new Error(`Image download failed (${response.status})`);
      blob = await response.blob();
    } else if (resolvedArtifact.kind === "image" && resolvedArtifact.data_base64) {
      const response = await fetch(`data:${resolvedArtifact.mime};base64,${resolvedArtifact.data_base64}`);
      blob = await response.blob();
    } else {
      const text = resolvedArtifact.kind === "table" ? tableText(resolvedArtifact, "csv") : textPayload(resolvedArtifact);
      blob = new Blob([text], { type: resolvedArtifact.kind === "svg" ? "image/svg+xml" : "text/plain;charset=utf-8" });
    }
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = downloadName(event);
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }

  return (
    <div className={`artifact-block-shell${inspect ? " has-inspect" : ""}`}>
      <section
        className={`artifact-block${oversized ? " is-compact" : ""}`}
        data-artifact-compact={oversized || undefined}
        data-artifact-id={event.artifact_id}
        data-artifact-kind={resolvedArtifact.kind}
        data-artifact-render-status={renderFailure ? "failed" : undefined}
      >
        <header className="artifact-header">
          <div className="artifact-heading">
            <Icon size={14} />
            <div>
              <div className="artifact-title">{event.title || resolvedArtifact.kind}</div>
              {event.caption ? <div className="artifact-caption">{event.caption}</div> : null}
            </div>
            {rowCount !== null ? <span className="artifact-count tabular-nums">{rowCount} rows</span> : null}
          </div>
          <div className="artifact-actions">
            {oversized && onOpen ? (
              <button className="artifact-action artifact-open-panel" type="button" onClick={() => onOpen?.(event)}>
                <PanelRightOpen size={12} /> Open in panel
              </button>
            ) : null}
            {resolvedArtifact.kind === "table" ? (
              <TableCopyMenu artifact={resolvedArtifact} onCopied={showCopied} />
            ) : (
              <button className="artifact-action" type="button" onClick={() => void copy()}>
                <Copy size={12} /> {copied ? "Copied" : "Copy"}
              </button>
            )}
            <button className="artifact-action" type="button" onClick={() => void download()}>
              <Download size={12} /> Download
            </button>
            <button
              aria-expanded={inspect}
              className={`artifact-action${inspect ? " is-active" : ""}`}
              type="button"
              onClick={() => setInspect((value) => !value)}
            >
              <Info size={12} /> Inspect
            </button>
          </div>
        </header>
        <div className="artifact-body" onClick={oversized && onOpen ? () => onOpen(event) : undefined}>
          {oversized ? (
            <CompactPreview artifact={resolvedArtifact} event={event} onRenderError={reportRenderFailure} ticket={ticket} />
          ) : (
            <ArtifactRenderer
              artifact={resolvedArtifact}
              event={event}
              ticket={ticket}
              onRenderError={reportRenderFailure}
              onImageLoad={(image) => setImageBounds({ width: image.naturalWidth, height: image.naturalHeight })}
            />
          )}
        </div>
        {renderFailure ? (
          <div className="artifact-render-failure" role="status">
            {deliveryStatus === "failed"
              ? "Render failed; agent unavailable."
              : deliveryStatus === "pending"
                ? "Render failed; reporting to agent…"
                : deliveryStatus === "queued"
                  ? "Render failed; diagnostic queued for agent."
                  : deliveryStatus === "deduplicated"
                    ? "Render failed; diagnostic already reported."
                    : "Render failed; diagnostic sent to agent."}
          </div>
        ) : null}
      </section>
      {inspect ? (
        <aside aria-label="Artifact inspector" className="artifact-inspect-panel">
          <header>
            <span>Artifact event</span>
            <button aria-label="Close artifact inspector" type="button" onClick={() => setInspect(false)}><X size={13} /></button>
          </header>
          <pre>{JSON.stringify(event, null, 2)}</pre>
        </aside>
      ) : null}
    </div>
  );
}
