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
      {/* compact forces explicit width/height from the viewBox — a viewBox-only
          SVG otherwise resolves to a zero-sized box inside the pan-zoom canvas
          (width: max-content parent × max-width: 100% child). */}
      <SvgRenderer compact source={source} />
    </PanZoomCanvas>
  );
}
