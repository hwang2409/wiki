import type { ArtifactViewState } from "../transcript-store";
import { SvgRenderer } from "../artifact-renderers";
import { PanZoomCanvas } from "./shared";

export function SvgArtifactDetail({
  onChange,
  source,
  state,
}: {
  onChange: (state: ArtifactViewState) => void;
  source: string;
  state: ArtifactViewState;
}) {
  return (
    <PanZoomCanvas label="SVG pan and zoom canvas" onChange={onChange} state={state}>
      <SvgRenderer source={source} />
    </PanZoomCanvas>
  );
}
