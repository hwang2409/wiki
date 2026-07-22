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
