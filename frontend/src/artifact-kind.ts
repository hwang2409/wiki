import type { ArtifactKind, SessionArtifact } from "./api";

const DIFF_HEAD_PATTERN = /^\s*(?:diff --git |--- [ab]?\/|\*\*\* )/m;

export function looksLikeUnifiedDiff(text: string | undefined | null): boolean {
  if (!text) return false;
  return DIFF_HEAD_PATTERN.test(text);
}

export function classifyArtifact(artifact: SessionArtifact): ArtifactKind {
  if (artifact.kind === "diff") return "diff";
  if (artifact.kind === "code" && looksLikeUnifiedDiff(artifact.source)) return "diff";
  return artifact.kind;
}

const KIND_LABELS: Record<string, string> = {
  mermaid: "Diagram",
  svg: "Image",
  image: "Image",
  table: "Table",
  plot: "Plot",
  code: "Code",
  diff: "Diff",
  "file-list": "File list",
  json: "JSON",
  pdf: "PDF",
};

export function humanizeArtifactKind(kind: string | undefined): string {
  if (!kind) return "Artifact";
  return KIND_LABELS[kind] ?? kind.charAt(0).toUpperCase() + kind.slice(1);
}
