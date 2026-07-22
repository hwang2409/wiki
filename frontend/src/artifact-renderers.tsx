import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ChevronDown, Copy, FileJson } from "lucide-react";
import type {
  ArtifactColumn,
  ArtifactFileEntry,
  SessionArtifact,
  SessionEvent,
} from "./api";
import { classifyArtifact } from "./artifact-kind";
import {
  ImageRenderer,
  MermaidRenderer,
  PlotRenderer,
  SvgRenderer,
  artifactUrl,
  type ArtifactRenderFailure,
} from "./artifact-block";
import { DiffPatchView } from "./diff-view";
import { ShikiCode } from "./shiki";
import { StatusBadge, statusToTone } from "./status-badge";

const TABLE_ROW_HEIGHT = 32;
const TABLE_VIEWPORT_HEIGHT = 320;
const TABLE_OVERSCAN = 8;

export type ArtifactRendererProps = {
  artifact: SessionArtifact;
  compact?: boolean;
  event: SessionEvent;
  onImageLoad?: (image: HTMLImageElement) => void;
  onOpenFile?: (entry: ArtifactFileEntry) => void;
  onRenderError?: (failure: ArtifactRenderFailure) => void;
  ticket: string;
};

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
