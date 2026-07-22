import { useState } from "react";
import { Hash } from "lucide-react";
import type { SessionArtifact } from "../api";
import { DiffPatchView } from "../diff-view";

export function DiffArtifactDetail({ artifact }: { artifact: SessionArtifact }) {
  const [showLineNumbers, setShowLineNumbers] = useState(false);
  return (
    <div className="artifact-detail-diff">
      <div className="artifact-detail-toolbar">
        <button
          aria-pressed={showLineNumbers}
          data-diff-line-numbers-toggle
          type="button"
          onClick={() => setShowLineNumbers((value) => !value)}
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
