import type { ArtifactViewState } from "../transcript-store";
import { MermaidRenderer } from "../artifact-block";
import { PanZoomCanvas } from "./shared";

export function MermaidArtifactDetail({
  onChange,
  source,
  state,
}: {
  onChange: (state: ArtifactViewState) => void;
  source: string;
  state: ArtifactViewState;
}) {
  return (
    <PanZoomCanvas label="Mermaid pan and zoom canvas" onChange={onChange} state={state}>
      <MermaidRenderer source={source} />
    </PanZoomCanvas>
  );
}
