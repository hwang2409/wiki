import type { ReactNode } from "react";

import { renderAnsi } from "./ansi";

export type HarnessOutputSegment = {
  kind: "text" | "error" | "note";
  text: string;
};

type StructuredToolInput = Record<string, unknown>;

export type StructuredEditPayload = {
  file_path?: string;
  old_string?: string;
  new_string?: string;
  replace_all?: boolean;
  patch?: string;
};

const DIFF_CONTEXT_LINES = 3;
const MAX_DIFF_LINES = 200;
const MAX_DIFF_CHARS = 60_000;
const MAX_DIFF_LINE_CHARS = 256;

function structuredToolInput(input: unknown): StructuredToolInput | null {
  if (input && typeof input === "object" && !Array.isArray(input)) {
    return input as StructuredToolInput;
  }
  if (typeof input !== "string") return null;
  const trimmed = input.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed as StructuredToolInput
      : null;
  } catch {
    return null;
  }
}

function stringField(input: StructuredToolInput | null, ...keys: string[]): string | null {
  if (!input) return null;
  for (const key of keys) {
    if (typeof input[key] === "string") return input[key] as string;
  }
  return null;
}

function diffLine(text: string): string {
  return text.length > MAX_DIFF_LINE_CHARS
    ? `${text.slice(0, MAX_DIFF_LINE_CHARS)}… [line truncated]`
    : text;
}

export function replacementDiff(filePath: string, oldText: string, newText: string): string | null {
  const oldLines = oldText ? oldText.split("\n") : [];
  const newLines = newText ? newText.split("\n") : [];
  let prefix = 0;
  while (
    prefix < oldLines.length
    && prefix < newLines.length
    && oldLines[prefix] === newLines[prefix]
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < oldLines.length - prefix
    && suffix < newLines.length - prefix
    && oldLines[oldLines.length - 1 - suffix] === newLines[newLines.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  if (prefix === oldLines.length && prefix === newLines.length) return null;
  const contextBefore = oldLines
    .slice(Math.max(0, prefix - DIFF_CONTEXT_LINES), prefix)
    .map((line) => ` ${diffLine(line)}`);
  const contextAfter = oldLines
    .slice(oldLines.length - suffix, oldLines.length - suffix + DIFF_CONTEXT_LINES)
    .map((line) => ` ${diffLine(line)}`);
  let removed = oldLines
    .slice(prefix, oldLines.length - suffix)
    .map((line) => `-${diffLine(line)}`);
  let added = newLines
    .slice(prefix, newLines.length - suffix)
    .map((line) => `+${diffLine(line)}`);
  const available = Math.max(2, MAX_DIFF_LINES - contextBefore.length - contextAfter.length);
  if (removed.length + added.length > available) {
    const removedLimit = Math.max(1, Math.floor(available / 2));
    const addedLimit = Math.max(1, available - removedLimit);
    removed = removed.slice(0, removedLimit);
    added = added.slice(0, addedLimit);
    if (removed.length < oldLines.length - prefix - suffix) removed.push("-… [diff truncated]");
    if (added.length < newLines.length - prefix - suffix) added.push("+… [diff truncated]");
  }
  const oldStart = oldLines.length > 0 ? Math.max(1, prefix - contextBefore.length + 1) : 0;
  const newStart = newLines.length > 0 ? Math.max(1, prefix - contextBefore.length + 1) : 0;
  const oldCount = contextBefore.length + removed.length + contextAfter.length;
  const newCount = contextBefore.length + added.length + contextAfter.length;
  const result = [
    `--- a/${filePath}`,
    `+++ b/${filePath}`,
    `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`,
    ...contextBefore,
    ...removed,
    ...added,
    ...contextAfter,
  ].join("\n");
  return result.length <= MAX_DIFF_CHARS ? result : null;
}

type PatchSection = {
  kind: "add" | "delete" | "update";
  path: string;
  lines: string[];
};

function normalizedHunkHeader(header: string, body: string[], kind: PatchSection["kind"]): string {
  if (/^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@(?:.*)$/.test(header)) return header;
  const oldCount = body.filter((line) => !line.startsWith("+")).length;
  const newCount = body.filter((line) => !line.startsWith("-")).length;
  const oldStart = kind === "add" ? 0 : 1;
  const newStart = kind === "delete" ? 0 : 1;
  return `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`;
}

function validHunk(header: string, body: string[]): boolean {
  const match = /^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@(?:.*)$/.exec(header);
  if (!match) return header.trim() === "@@";
  if (!body.some((line) => line.startsWith("+") || line.startsWith("-"))) return false;
  const oldCount = body.filter((line) => line.startsWith(" ") || line.startsWith("-")).length;
  const newCount = body.filter((line) => line.startsWith(" ") || line.startsWith("+")).length;
  return oldCount === Number(match[2] ?? 1) && newCount === Number(match[4] ?? 1);
}

function validHunkBody(body: string[]): boolean {
  return body.length > 0
    && body.every((line) => line.startsWith(" ") || line.startsWith("+") || line.startsWith("-") || line.startsWith("\\"))
    && body.some((line) => line.startsWith("+") || line.startsWith("-"));
}

function validateUnifiedPatch(source: string): boolean {
  if (source.length > MAX_DIFF_CHARS) return false;
  const lines = source.split("\n");
  if (lines.at(-1) === "") lines.pop();
  const fileIndexes = lines
    .map((line, index) => /^---\s+\S/.test(line) ? index : -1)
    .filter((index) => index >= 0);
  if (fileIndexes.length === 0) return false;
  for (let fileIndex = 0; fileIndex < fileIndexes.length; fileIndex += 1) {
    const start = fileIndexes[fileIndex];
    const end = fileIndexes[fileIndex + 1] ?? lines.length;
    if (!/^\+\+\+\s+\S/.test(lines[start + 1] ?? "")) return false;
    const body = lines.slice(start + 2, end);
    const hunkIndexes = body
      .map((line, index) => line.startsWith("@@") ? index : -1)
      .filter((index) => index >= 0);
    if (hunkIndexes.length === 0) return false;
    for (let hunkIndex = 0; hunkIndex < hunkIndexes.length; hunkIndex += 1) {
      const hunkStart = hunkIndexes[hunkIndex];
      const hunkEnd = hunkIndexes[hunkIndex + 1] ?? body.length;
      const hunkHeader = body[hunkStart];
      const hunkBody = body.slice(hunkStart + 1, hunkEnd);
      if (!validHunk(hunkHeader, hunkBody) || !validHunkBody(hunkBody)) return false;
    }
  }
  return true;
}

function normalizedPatchSection(section: PatchSection): string[] {
  const output = [
    section.kind === "add" ? "--- /dev/null" : `--- a/${section.path}`,
    section.kind === "delete" ? "+++ /dev/null" : `+++ b/${section.path}`,
  ];
  const hunkIndexes = section.lines
    .map((line, index) => line.startsWith("@@") ? index : -1)
    .filter((index) => index >= 0);
  if (hunkIndexes.length === 0) {
    output.push(
      normalizedHunkHeader("@@", section.lines, section.kind),
      ...section.lines,
    );
    return output;
  }
  for (let index = 0; index < hunkIndexes.length; index += 1) {
    const start = hunkIndexes[index];
    const end = hunkIndexes[index + 1] ?? section.lines.length;
    const header = section.lines[start];
    const body = section.lines.slice(start + 1, end);
    output.push(normalizedHunkHeader(header, body, section.kind), ...body);
  }
  return output;
}

function normalizeApplyPatch(source: string): string | null {
  const lines = source.split("\n");
  if (lines.at(-1) === "") lines.pop();
  if (lines.shift() !== "*** Begin Patch" || lines.pop() !== "*** End Patch") return null;
  const sections: PatchSection[] = [];
  let current: PatchSection | null = null;
  const finish = () => {
    if (current) sections.push(current);
    current = null;
  };
  for (const line of lines) {
    const marker = /^\*\*\* (Add|Delete|Update) File:\s*(.+)$/.exec(line);
    if (marker) {
      finish();
      current = {
        kind: marker[1].toLowerCase() as PatchSection["kind"],
        path: marker[2].trim(),
        lines: [],
      };
      continue;
    }
    if (current) current.lines.push(line);
  }
  finish();
  if (sections.length === 0) return null;
  if (sections.some((section) => {
    if (!section.path || !validHunkBody(section.lines.filter((line) => !line.startsWith("@@")))) return true;
    const hunkIndexes = section.lines
      .map((line, index) => line.startsWith("@@") ? index : -1)
      .filter((index) => index >= 0);
    return hunkIndexes.some((start, index) => {
      const end = hunkIndexes[index + 1] ?? section.lines.length;
      return !validHunk(section.lines[start], section.lines.slice(start + 1, end))
        || !validHunkBody(section.lines.slice(start + 1, end));
    });
  })) return null;
  const normalized = sections.flatMap(normalizedPatchSection).join("\n");
  return validateUnifiedPatch(normalized) ? normalized : null;
}

export function editDiffFromInput(
  name: string,
  input: unknown,
  editPayload?: StructuredEditPayload,
): string | null {
  const structured = editPayload ?? structuredToolInput(input);
  const nestedPatch = stringField(structured, "patch", "diff");
  const source = nestedPatch ?? (typeof input === "string" ? input : "");
  const lowerName = name.trim().toLowerCase();
  if (lowerName === "apply_patch" || source.includes("*** Begin Patch")) {
    if (source.startsWith("diff --git ") || source.startsWith("--- ")) {
      return validateUnifiedPatch(source) ? source : null;
    }
    return normalizeApplyPatch(source);
  }
  const oldText = stringField(structured, "old_string", "oldString");
  const newText = stringField(structured, "new_string", "newString");
  const filePath = stringField(structured, "file_path", "filePath", "path");
  if (oldText === null || newText === null || !filePath) return null;
  return replacementDiff(filePath, oldText, newText);
}

const HARNESS_TAG = /<(tool_use_error|system-reminder)>([\s\S]*?)<\/\1>/gi;
const BOILERPLATE = /\s*\(file state is current in your context\s*[—-]\s*no need to read it back\)\s*$/i;

function stripBoilerplate(text: string): string {
  return text.replace(BOILERPLATE, "");
}

export function parseHarnessOutput(raw: string): HarnessOutputSegment[] {
  const segments: HarnessOutputSegment[] = [];
  let cursor = 0;
  for (const match of raw.matchAll(HARNESS_TAG)) {
    const index = match.index ?? 0;
    const plain = stripBoilerplate(raw.slice(cursor, index));
    if (plain) segments.push({ kind: "text", text: plain });
    const inner = stripBoilerplate(match[2] ?? "").trim();
    if (inner) {
      segments.push({
        kind: match[1].toLowerCase() === "tool_use_error" ? "error" : "note",
        text: inner,
      });
    }
    cursor = index + match[0].length;
  }
  const tail = stripBoilerplate(raw.slice(cursor));
  if (tail) segments.push({ kind: "text", text: tail });
  return segments;
}

export function cleanHarnessOutput(raw: string): string {
  return parseHarnessOutput(raw).map((segment) => segment.text).join("\n");
}

export function hasHarnessError(raw: string): boolean {
  return parseHarnessOutput(raw).some((segment) => segment.kind === "error");
}

export function segmentsForDisplayText(
  segments: HarnessOutputSegment[],
  displayText: string,
): HarnessOutputSegment[] {
  const fullText = segments.map((segment) => segment.text).join("\n");
  if (displayText === fullText) return segments;
  return segments.flatMap((segment, index) => {
    const start = segments
      .slice(0, index)
      .reduce((offset, previous) => offset + previous.text.length + 1, 0);
    const visibleEnd = Math.max(0, Math.min(segment.text.length, displayText.length - start));
    const text = visibleEnd > 0 ? segment.text.slice(0, visibleEnd) : "";
    return text ? [{ ...segment, text }] : [];
  });
}

export function HarnessOutput({
  text,
  segments,
  ansi = false,
}: {
  text?: string;
  segments?: HarnessOutputSegment[];
  ansi?: boolean;
}): ReactNode {
  const parsed = segments ?? parseHarnessOutput(text ?? "");
  return parsed.map((segment, index) => (
    <span key={`${segment.kind}:${index}`}>
      {index > 0 ? "\n" : null}
      <span className={`session-output-segment is-${segment.kind}`}>
        {ansi ? renderAnsi(segment.text) : segment.text}
      </span>
    </span>
  ));
}
