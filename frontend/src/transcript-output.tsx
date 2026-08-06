import type { ReactNode } from "react";

import { renderAnsi } from "./ansi";

export type HarnessOutputSegment = {
  kind: "text" | "error" | "note";
  text: string;
};

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

export function HarnessOutput({ text, ansi = false }: { text: string; ansi?: boolean }): ReactNode {
  const segments = parseHarnessOutput(text);
  return segments.map((segment, index) => (
    <span key={`${segment.kind}:${index}`}>
      {index > 0 ? "\n" : null}
      <span className={`session-output-segment is-${segment.kind}`}>
        {ansi ? renderAnsi(segment.text) : segment.text}
      </span>
    </span>
  ));
}
