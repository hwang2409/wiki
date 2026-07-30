import type { ArtifactFileEntry, SessionArtifact, SessionEvent } from "./api";

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
