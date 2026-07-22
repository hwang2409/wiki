import type { SessionArtifact } from "../api";

export function JsonArtifactDetail({ artifact }: { artifact: SessionArtifact }) {
  const value = artifact.json_data !== undefined ? artifact.json_data : artifact.source;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  return (
    <div className="artifact-detail-json">
      <pre className="artifact-json is-detail">
        <code>{text}</code>
      </pre>
    </div>
  );
}
