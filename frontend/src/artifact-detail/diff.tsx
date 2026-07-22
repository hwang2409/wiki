import type { SessionArtifact } from "../api";
import { SplitDiffView } from "../split-diff";

export function DiffArtifactDetail({ artifact }: { artifact: SessionArtifact }) {
  return (
    <div className="artifact-detail-diff">
      <SplitDiffView
        className="artifact-diff"
        emptyClassName="artifact-diff-empty"
        emptyMessage="No diff to display."
        patch={artifact.source ?? ""}
        viewType="split"
      />
    </div>
  );
}
