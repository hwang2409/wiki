import { useState } from "react";
import { RotateCcw } from "lucide-react";
import { PlotRenderer } from "../artifact-block";

export function PlotArtifactDetail({ spec }: { spec: Record<string, unknown> }) {
  const [renderKey, setRenderKey] = useState(0);
  return (
    <div className="artifact-plot-detail">
      <div className="artifact-detail-toolbar">
        <button type="button" onClick={() => setRenderKey((key) => key + 1)}>
          <RotateCcw size={12} /> Reset zoom
        </button>
      </div>
      <div className="artifact-plot-detail-canvas">
        <PlotRenderer key={renderKey} spec={spec} actions />
      </div>
    </div>
  );
}
