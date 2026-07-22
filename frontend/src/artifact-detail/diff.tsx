import { Hash } from "lucide-react";
import type { SessionArtifact } from "../api";
import { DiffPatchView } from "../diff-view";
import type { ArtifactViewState } from "../transcript-store";

export function DiffArtifactDetail({
  artifact,
  onChange,
  state,
}: {
  artifact: SessionArtifact;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
}) {
  const showLineNumbers = state.showLineNumbers ?? false;
  return (
    <div className="artifact-detail-diff">
      <div className="artifact-detail-toolbar">
        <button
          aria-pressed={showLineNumbers}
          data-diff-line-numbers-toggle
          type="button"
          onClick={() => onChange({ ...state, showLineNumbers: !showLineNumbers })}
        >
          <Hash size={12} />
          {showLineNumbers ? "Hide line numbers" : "Show line numbers"}
        </button>
      </div>
      <div className="artifact-detail-diff-scroll">
        <DiffPatchView source={artifact.source ?? ""} showLineNumbers={showLineNumbers} />
      </div>
    </div>
  );
}
