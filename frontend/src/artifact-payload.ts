import type { SessionArtifact, SessionEvent } from "./api";
import { classifyArtifact } from "./artifact-kind";
import { artifactUrl, tableText } from "./artifact-renderers";

export function textPayload(artifact: SessionArtifact): string {
  switch (artifact.kind) {
    case "mermaid":
    case "svg":
    case "code":
    case "diff":
      return artifact.source ?? "";
    case "table":
      return tableText(artifact, "tsv");
    case "plot":
      return JSON.stringify(artifact.spec_vega_lite ?? {}, null, 2);
    case "image":
      return artifact.data_base64 ?? artifact.ref ?? "";
    case "file-list":
      return (artifact.files ?? []).map((entry) => entry.path).join("\n");
    case "json":
      return typeof artifact.json_data === "string"
        ? artifact.json_data
        : JSON.stringify(artifact.json_data ?? {}, null, 2);
    case "pdf":
    case "video":
    case "audio":
      return artifact.ref ?? "";
    case "visual-diff":
      return JSON.stringify(
        { before: artifact.before?.ref ?? "", after: artifact.after?.ref ?? "" },
        null,
        2,
      );
  }
}

export async function imageBase64(url: string): Promise<string> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Image download failed (${response.status})`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
  }
  return btoa(binary);
}

export function downloadName(event: SessionEvent): string {
  const artifact = event.artifact!;
  const effectiveKind = classifyArtifact(artifact);
  const base = (event.title || `artifact-${event.artifact_id?.slice(0, 8) || effectiveKind}`)
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "artifact";
  if (effectiveKind === "code" && artifact.filename) {
    return artifact.filename.split(/[\\/]/).pop() || `${base}.txt`;
  }
  const videoExtension = artifact.mime === "image/gif" ? "gif" : "mp4";
  const audioExtension = artifact.mime === "audio/mpeg" ? "mp3" : "wav";
  const extension = {
    mermaid: "mmd",
    svg: "svg",
    image: artifact.mime === "image/jpeg" ? "jpg" : artifact.mime?.split("/")[1] || "png",
    table: "csv",
    plot: "json",
    code: artifact.language?.replace(/[^a-zA-Z0-9]/g, "") || "txt",
    diff: "diff",
    "file-list": "txt",
    json: "json",
    pdf: "pdf",
    video: videoExtension,
    audio: audioExtension,
    "visual-diff": "json",
  }[effectiveKind];
  return `${base}.${extension}`;
}

function variantExtension(mime: string | undefined): string {
  if (mime === "image/jpeg") return "jpg";
  return mime?.split("/")[1] || "png";
}

async function downloadVariant(url: string, name: string): Promise<void> {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Variant download failed (${response.status})`);
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = name;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
}

export async function downloadArtifact(ticket: string, event: SessionEvent): Promise<void> {
  const artifact = event.artifact!;
  if (artifact.kind === "visual-diff") {
    const base = artifactUrl(ticket, event);
    const stem = downloadName(event).replace(/\.json$/, "");
    const beforeExt = variantExtension(artifact.before?.mime);
    const afterExt = variantExtension(artifact.after?.mime);
    await downloadVariant(`${base}?variant=before`, `${stem}.before.${beforeExt}`);
    await downloadVariant(`${base}?variant=after`, `${stem}.after.${afterExt}`);
    return;
  }
  let blob: Blob;
  if (artifact.kind === "pdf" || artifact.kind === "video" || artifact.kind === "audio") {
    const response = artifact.data_base64
      ? await fetch(`data:${artifact.mime};base64,${artifact.data_base64}`)
      : await fetch(artifactUrl(ticket, event));
    if (!response.ok) throw new Error(`${artifact.kind} download failed (${response.status})`);
    blob = await response.blob();
  } else if (artifact.kind === "image" && !artifact.data_base64) {
    const response = await fetch(artifactUrl(ticket, event));
    if (!response.ok) throw new Error(`Image download failed (${response.status})`);
    blob = await response.blob();
  } else if (artifact.kind === "image" && artifact.data_base64) {
    const response = await fetch(`data:${artifact.mime};base64,${artifact.data_base64}`);
    blob = await response.blob();
  } else {
    const text = artifact.kind === "table" ? tableText(artifact, "csv") : textPayload(artifact);
    blob = new Blob([text], { type: artifact.kind === "svg" ? "image/svg+xml" : "text/plain;charset=utf-8" });
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = downloadName(event);
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}
