import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  BarChart3,
  Code2,
  Columns2,
  Copy,
  Download,
  FileJson,
  FileText,
  GitBranch,
  Image as ImageIcon,
  Info,
  Maximize2,
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
import { downloadArtifact, imageBase64, textPayload } from "./artifact-payload";
import { ArtifactError } from "./artifact-state";
import {
  ArtifactRenderer,
  CompactPreview,
  TableCopyMenu,
  artifactUrl,
  svgBounds,
  type ArtifactRenderFailure,
} from "./artifact-renderers";
import {
  readInlineArtifactState,
  subscribeInlineArtifactState,
  writeInlineArtifactState,
} from "./transcript-store";


export const KIND_ICONS: Record<ArtifactKind, LucideIcon> = {
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
  "visual-diff": Columns2,
};


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
    case "visual-diff": {
      const width = artifact.before?.width ?? artifact.after?.width ?? 0;
      const height = artifact.before?.height ?? artifact.after?.height ?? 0;
      return width > 400 || height > 400;
    }
  }
}

// Per-session-view set of artifact IDs that have already mounted once.
// First mount for a given ID plays the subtle enter animation; subsequent
// mounts (row virtualization scrolling back to a row, for example) do
// not — otherwise fast scrolls would strobe. Scoped by session key so
// switching sessions plays the animation again for those artifacts;
// bounded per scope so a long-running session doesn't grow the set
// unbounded. WIKI-200.
const SEEN_ARTIFACT_MAX_PER_SCOPE = 512;
const SEEN_ARTIFACT_MAX_SCOPES = 64;
const seenArtifactByScope = new Map<string, Set<string>>();

function markSeen(scope: string, artifactId: string): boolean {
  let scoped = seenArtifactByScope.get(scope);
  if (!scoped) {
    scoped = new Set<string>();
    seenArtifactByScope.set(scope, scoped);
  } else {
    // Bump the session scope so the LRU eviction below drops the coldest
    // session, not the one used most recently.
    seenArtifactByScope.delete(scope);
    seenArtifactByScope.set(scope, scoped);
  }
  if (scoped.has(artifactId)) {
    // Bump insertion order so the LRU eviction below drops truly cold
    // entries rather than active ones.
    scoped.delete(artifactId);
    scoped.add(artifactId);
    return false;
  }
  scoped.add(artifactId);
  while (scoped.size > SEEN_ARTIFACT_MAX_PER_SCOPE) {
    const oldest = scoped.values().next();
    if (oldest.done) break;
    scoped.delete(oldest.value);
  }
  while (seenArtifactByScope.size > SEEN_ARTIFACT_MAX_SCOPES) {
    const oldestScope = seenArtifactByScope.keys().next();
    if (oldestScope.done) break;
    seenArtifactByScope.delete(oldestScope.value);
  }
  return true;
}

// Test hook — never call from production code.
export function __resetSeenArtifactsForTests(): void {
  seenArtifactByScope.clear();
}

export function __markSeenArtifactForTests(scope: string, artifactId: string): boolean {
  return markSeen(scope, artifactId);
}

export function __seenArtifactScopeCountForTests(): number {
  return seenArtifactByScope.size;
}

function useIsFreshArtifact(scope: string, artifactId: string | undefined): boolean {
  const [fresh] = useState(() => {
    if (!artifactId) return false;
    return markSeen(scope, artifactId);
  });
  return fresh;
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
  onInspect,
  onOpen,
  sessionKey,
  ticket,
}: {
  event: SessionEvent;
  onInspect?: (event: SessionEvent) => void;
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
  const isEntering = useIsFreshArtifact(inlineSessionKey, event.artifact_id);
  const shellClass = `artifact-block-shell${inspect ? " has-inspect" : ""}${isEntering ? " is-entering" : ""}`;
  if (!artifact) {
    return (
      <div className={shellClass}>
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
    await downloadArtifact(ticket, event);
  }

  return (
    <div className={shellClass}>
      <section
        className={`artifact-block${showCompact ? " is-compact" : ""}${inlineExpanded ? " is-expanded" : ""}`}
        data-artifact-compact={showCompact || undefined}
        data-artifact-expanded={inlineExpanded || undefined}
        data-artifact-id={event.artifact_id}
        data-artifact-kind={resolvedArtifact.kind}
        data-artifact-render-status={renderFailure ? "failed" : undefined}
        tabIndex={onInspect ? 0 : undefined}
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
            ) : resolvedArtifact.kind === "video" || resolvedArtifact.kind === "audio" ? null : (
              <button className="artifact-action" type="button" onClick={() => void copy()}>
                <Copy aria-hidden="true" size={12} />
                <span className="artifact-action-label">{copied ? "Copied" : "Copy"}</span>
              </button>
            )}
            <button className="artifact-action" type="button" onClick={() => void download()}>
              <Download aria-hidden="true" size={12} />
              <span className="artifact-action-label">Download</span>
            </button>
            {onInspect ? (
              <button
                className="artifact-action artifact-action-fullscreen"
                title="Fullscreen (⌘↩)"
                type="button"
                onClick={() => onInspect(event)}
              >
                <Maximize2 aria-hidden="true" size={12} />
                <span className="artifact-action-label">Fullscreen</span>
              </button>
            ) : null}
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
              onExpand={onInspect ? () => onInspect(event) : undefined}
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
