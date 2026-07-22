import type { SessionArtifact } from "../api";
import { DiffPatchView } from "../diff-view";

export function DiffArtifactDetail({ artifact }: { artifact: SessionArtifact }) {
  return (
    <div className="artifact-detail-diff">
      <DiffPatchView source={artifact.source ?? ""} showLineNumbers />
    </div>
  );
}
