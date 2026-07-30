import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  BarChart3,
  Code2,
  Copy,
  Download,
  FileJson,
  FileText,
  GitBranch,
  Image as ImageIcon,
  Info,
  Music,
  PanelRightOpen,
  Shapes,
  Table2,
  Video,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { sendAgentMessage } from "./api";
import type {
  ArtifactKind,
  SessionArtifact,
  SessionEvent,
} from "./api";
import { classifyArtifact } from "./artifact-kind";
import { ArtifactError } from "./artifact-state";
import {
  ArtifactRenderer,
  CompactPreview,
  TableCopyMenu,
  artifactUrl,
  svgBounds,
  tableText,
  type ArtifactRenderFailure,
} from "./artifact-renderers";
import {
  readInlineArtifactState,
  subscribeInlineArtifactState,
  writeInlineArtifactState,
} from "./transcript-store";


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
  pdf: FileText,
  video: Video,
  audio: Music,
};


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
    case "pdf":
    case "video":
    case "audio":
      return artifact.ref ?? "";
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
  const videoExtension = artifact.mime === "image/gif" ? "gif" : "mp4";
  const audioExtension = artifact.mime === "audio/mpeg" ? "mp3" : "wav";
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
    pdf: "pdf",
    video: videoExtension,
    audio: audioExtension,
  }[effectiveKind];
  return `${base}.${extension}`;
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
    case "pdf": return true;
    case "video": return false;
    case "audio": return false;
  }
}

function useInlineExpanded(sessionKey: string, artifactId: string | undefined): [boolean, (next: boolean) => void] {
  const id = artifactId ?? "";
  const state = useSyncExternalStore<{ expanded?: boolean }>(
    (listener) => id ? subscribeInlineArtifactState(sessionKey, id, listener) : () => {},
    () => (id ? readInlineArtifactState(sessionKey, id) : {}),
    () => ({}),
  );
  const setExpanded = useCallback(
    (next: boolean) => {
      if (!id) return;
      writeInlineArtifactState(sessionKey, id, { ...readInlineArtifactState(sessionKey, id), expanded: next });
    },
    [id, sessionKey],
  );
  return [Boolean(state.expanded), setExpanded];
}


export function ArtifactBlock({
  event,
  onOpen,
  sessionKey,
  ticket,
}: {
  event: SessionEvent;
  onOpen?: (event: SessionEvent) => void;
  sessionKey?: string;
  ticket: string;
}) {
  const artifact = event.artifact;
  const inlineSessionKey = sessionKey ?? ticket;
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
  const [inlineExpanded, setInlineExpanded] = useInlineExpanded(inlineSessionKey, event.artifact_id);
  if (!artifact) {
    return (
      <div className="artifact-block-shell">
        <ArtifactError title="Artifact payload missing." />
      </div>
    );
  }
  const resolvedArtifact = artifact;
  const Icon = KIND_ICONS[resolvedArtifact.kind] ?? FileJson;
  const rowCount = resolvedArtifact.kind === "table" ? resolvedArtifact.rows?.length ?? 0 : null;
  const oversized = artifactExceedsInlineThreshold(resolvedArtifact, imageBounds);
  const showCompact = oversized && !inlineExpanded;
  const primaryTitle = event.title || resolvedArtifact.filename || null;
  const headingTitle = primaryTitle || resolvedArtifact.kind;
  const descriptionText = event.caption || (primaryTitle && resolvedArtifact.filename && resolvedArtifact.filename !== primaryTitle ? resolvedArtifact.filename : null);

  function showCopied() {
    setCopied(true);
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
    copiedTimer.current = window.setTimeout(() => setCopied(false), 1400);
  }

  async function copy() {
    const needsBinaryFetch =
      (resolvedArtifact.kind === "image" && !resolvedArtifact.data_base64) ||
      resolvedArtifact.kind === "pdf";
    const value = needsBinaryFetch
      ? await imageBase64(artifactUrl(ticket, event))
      : textPayload(resolvedArtifact);
    await navigator.clipboard.writeText(value);
    showCopied();
  }

  async function download() {
    const binaryKinds = new Set(["pdf", "video", "audio"]);
    let blob: Blob;
    if (binaryKinds.has(resolvedArtifact.kind)) {
      if (resolvedArtifact.data_base64) {
        const response = await fetch(
          `data:${resolvedArtifact.mime};base64,${resolvedArtifact.data_base64}`,
        );
        blob = await response.blob();
      } else {
        const response = await fetch(artifactUrl(ticket, event));
        if (!response.ok) {
          throw new Error(
            `${resolvedArtifact.kind} download failed (${response.status})`,
          );
        }
        blob = await response.blob();
      }
    } else if (resolvedArtifact.kind === "image" && !resolvedArtifact.data_base64) {
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
        className={`artifact-block${showCompact ? " is-compact" : ""}${inlineExpanded ? " is-expanded" : ""}`}
        data-artifact-compact={showCompact || undefined}
        data-artifact-expanded={inlineExpanded || undefined}
        data-artifact-id={event.artifact_id}
        data-artifact-kind={resolvedArtifact.kind}
        data-artifact-render-status={renderFailure ? "failed" : undefined}
      >
        <header className="artifact-header">
          <div className="artifact-heading">
            <Icon aria-hidden="true" size={13} />
            <span className="artifact-title" title={headingTitle}>{headingTitle}</span>
            {descriptionText ? (
              <span className="artifact-caption" title={descriptionText}>{descriptionText}</span>
            ) : null}
            {rowCount !== null ? (
              <span className="artifact-count tabular-nums">{rowCount} rows</span>
            ) : null}
          </div>
          <div className="artifact-actions">
            {oversized ? (
              <button
                aria-expanded={inlineExpanded}
                className="artifact-action artifact-action-expand"
                type="button"
                onClick={() => setInlineExpanded(!inlineExpanded)}
              >
                {inlineExpanded ? "Show less" : "Show all"}
              </button>
            ) : null}
            {oversized && onOpen ? (
              <button
                className="artifact-action artifact-open-panel"
                type="button"
                onClick={() => onOpen?.(event)}
              >
                <PanelRightOpen aria-hidden="true" size={12} />
                <span className="artifact-action-label">Open in panel</span>
              </button>
            ) : null}
            {resolvedArtifact.kind === "table" ? (
              <TableCopyMenu artifact={resolvedArtifact} onCopied={showCopied} />
            ) : (
              <button className="artifact-action" type="button" onClick={() => void copy()}>
                <Copy aria-hidden="true" size={12} />
                <span className="artifact-action-label">{copied ? "Copied" : "Copy"}</span>
              </button>
            )}
            <button className="artifact-action" type="button" onClick={() => void download()}>
              <Download aria-hidden="true" size={12} />
              <span className="artifact-action-label">Download</span>
            </button>
            <button
              aria-expanded={inspect}
              className={`artifact-action${inspect ? " is-active" : ""}`}
              type="button"
              onClick={() => setInspect((value) => !value)}
            >
              <Info aria-hidden="true" size={12} />
              <span className="artifact-action-label">Inspect</span>
            </button>
          </div>
        </header>
        <div
          className="artifact-body"
          onClick={showCompact && onOpen ? () => onOpen(event) : undefined}
        >
          {showCompact ? (
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
