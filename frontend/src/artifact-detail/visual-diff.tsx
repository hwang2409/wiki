import type { SessionArtifact, SessionEvent } from "../api";
import { VisualDiffRenderer } from "../visual-diff-renderer";

export function VisualDiffArtifactDetail({
  artifact,
  event,
  ticket,
}: {
  artifact: SessionArtifact;
  event: SessionEvent;
  ticket: string;
}) {
  return (
    <div className="visual-diff-detail">
      <VisualDiffRenderer artifact={artifact} event={event} ticket={ticket} />
    </div>
  );
}
