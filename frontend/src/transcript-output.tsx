import type { ReactNode } from "react";

import { renderAnsi } from "./ansi";

export type HarnessOutputSegment = {
  kind: "text" | "error" | "note";
  text: string;
};

type StructuredToolInput = Record<string, unknown>;

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

function diffLineCount(text: string): number {
  return text ? text.split("\n").length : 0;
}

function replacementDiff(filePath: string, oldText: string, newText: string): string {
  const oldLines = oldText ? oldText.split("\n") : [];
  const newLines = newText ? newText.split("\n") : [];
  let prefix = 0;
  while (
    prefix < oldLines.length
    && prefix < newLines.length
    && oldLines[prefix] === newLines[prefix]
    && prefix < 3
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < oldLines.length - prefix
    && suffix < newLines.length - prefix
    && oldLines[oldLines.length - 1 - suffix] === newLines[newLines.length - 1 - suffix]
    && suffix < 3
  ) {
    suffix += 1;
  }
  const contextBefore = oldLines.slice(0, prefix).map((line) => ` ${line}`);
  const removed = oldLines.slice(prefix, oldLines.length - suffix).map((line) => `-${line}`);
  const added = newLines.slice(prefix, newLines.length - suffix).map((line) => `+${line}`);
  const contextAfter = suffix > 0
    ? oldLines.slice(oldLines.length - suffix).map((line) => ` ${line}`)
    : [];
  const oldStart = oldLines.length > 0 ? 1 : 0;
  const newStart = newLines.length > 0 ? 1 : 0;
  const oldCount = diffLineCount(oldText);
  const newCount = diffLineCount(newText);
  return [
    `--- a/${filePath}`,
    `+++ b/${filePath}`,
    `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`,
    ...contextBefore,
    ...removed,
    ...added,
    ...contextAfter,
  ].join("\n");
}

type PatchSection = {
  kind: "add" | "delete" | "update";
  path: string;
  lines: string[];
};

function normalizedHunkHeader(header: string, body: string[], kind: PatchSection["kind"]): string {
  if (/^@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@/.test(header)) return header;
  const oldCount = body.filter((line) => !line.startsWith("+")).length;
  const newCount = body.filter((line) => !line.startsWith("-")).length;
  const oldStart = kind === "add" ? 0 : 1;
  const newStart = kind === "delete" ? 0 : 1;
  return `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@`;
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
    if (line === "*** Begin Patch" || line === "*** End Patch") continue;
    if (current) current.lines.push(line);
  }
  finish();
  if (sections.length === 0) return null;
  return sections.flatMap(normalizedPatchSection).join("\n");
}

export function editDiffFromInput(name: string, input: unknown): string | null {
  const structured = structuredToolInput(input);
  const nestedPatch = stringField(structured, "patch", "diff");
  const source = nestedPatch ?? (typeof input === "string" ? input : "");
  const lowerName = name.trim().toLowerCase();
  if (lowerName === "apply_patch" || source.includes("*** Begin Patch")) {
    if (source.startsWith("diff --git ") || source.startsWith("--- ")) return source;
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
