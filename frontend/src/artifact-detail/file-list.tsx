import { FileJson } from "lucide-react";
import type { ArtifactFileEntry, SessionArtifact } from "../api";

export function FileListArtifactDetail({
  artifact,
  onOpenFile,
}: {
  artifact: SessionArtifact;
  onOpenFile?: (entry: ArtifactFileEntry) => void;
}) {
  const files = artifact.files ?? [];
  if (files.length === 0) {
    return (
      <div className="artifact-detail-file-list">
        <div className="artifact-file-list-empty">No files.</div>
      </div>
    );
  }
  return (
    <div className="artifact-detail-file-list">
    <ul className="artifact-file-list is-detail">
      {files.map((entry, index) => {
        const label = entry.label ?? entry.path;
        const clickable = Boolean(onOpenFile);
        const content = (
          <>
            <FileJson aria-hidden="true" className="artifact-file-list-icon" size={12} />
            <span className="artifact-file-list-label">{label}</span>
            {entry.status ? (
              <span className="artifact-file-list-status">{entry.status}</span>
            ) : null}
          </>
        );
        return (
          <li className="artifact-file-list-item" key={`${entry.path}-${index}`}>
            {clickable ? (
              <button
                className="artifact-file-list-button"
                onClick={() => onOpenFile?.(entry)}
                title={entry.path}
                type="button"
              >
                {content}
              </button>
            ) : (
              <span className="artifact-file-list-static" title={entry.path}>
                {content}
              </span>
            )}
          </li>
        );
      })}
    </ul>
    </div>
  );
}
