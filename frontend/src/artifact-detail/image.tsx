import type { SessionArtifact, SessionEvent } from "../api";
import type { ArtifactViewState } from "../transcript-store";
import { artifactUrl } from "../artifact-block";
import { PanZoomCanvas } from "./shared";

export function ImageArtifactDetail({
  artifact,
  event,
  onChange,
  state,
  ticket,
}: {
  artifact: SessionArtifact;
  event: SessionEvent;
  onChange: (state: ArtifactViewState) => void;
  state: ArtifactViewState;
  ticket: string;
}) {
  const source = artifact.data_base64
    ? `data:${artifact.mime ?? "image/png"};base64,${artifact.data_base64}`
    : artifactUrl(ticket, event);
  return (
    <PanZoomCanvas label="Image pan and zoom canvas" onChange={onChange} state={state}>
      <img alt={event.title || event.caption || "Agent artifact"} className="artifact-detail-image" src={source} />
    </PanZoomCanvas>
  );
}
