import type { SessionEvent, SessionTool } from "./api";
import { editDiffFromInput, editDiffIsTruncated } from "./transcript-output";

const UNIFIED_DIFF_HEAD = /^\s*(?:diff --git |--- [ab]?\/|\*\*\* )/m;

export type ToolPresentation = "inline" | "block" | "diff";

export type TraceRow =
  | { kind: "tool"; event: SessionEvent; eventIndex: number; withResult: boolean }
  | { kind: "thinking"; event: SessionEvent; eventIndex: number };

export function toolSummaryLine(tool: SessionTool): string {
  return tool.summary || tool.input.split("\n")[0].slice(0, 120);
}

export function toolSummaryParts(tool: SessionTool): { verb: string; target: string } {
  const summary = toolSummaryLine(tool).trim();
  const splitAt = summary.search(/\s/);
  if (splitAt < 0) return { verb: summary, target: "" };
  return { verb: summary.slice(0, splitAt), target: summary.slice(splitAt).trim() };
}

function toolName(tool: SessionTool): string {
  return tool.name.trim().toLowerCase();
}

export function isBashTool(tool: SessionTool): boolean {
  return toolName(tool) === "bash" || tool.archetype === "bash" || tool.archetype === "terminal";
}

export function isReadOrSearchTool(tool: SessionTool): boolean {
  return tool.archetype === "read"
    || tool.archetype === "search"
    || ["read", "notebookread", "grep", "glob"].includes(toolName(tool));
}

function isEditTool(tool: SessionTool): boolean {
  return tool.archetype === "edit"
    || ["edit", "multiedit", "notebookedit", "apply_patch"].includes(toolName(tool));
}

function isRichWriteOrTaskTool(tool: SessionTool): boolean {
  return ["write", "writefile", "task", "agent"].includes(toolName(tool))
    || tool.archetype === "agent";
}

export function looksLikeUnifiedDiff(text: string | null | undefined): boolean {
  return Boolean(text && UNIFIED_DIFF_HEAD.test(text));
}

export function toolDiffSource(tool: SessionTool, displayOutput = ""): string | null {
  if (isEditTool(tool)) {
    if (editDiffIsTruncated(tool.input, tool.edit)) return null;
    return editDiffFromInput(tool.name, tool.input, tool.edit)
      ?? (looksLikeUnifiedDiff(displayOutput) ? displayOutput : null);
  }
  return tool.archetype === "diff" && looksLikeUnifiedDiff(displayOutput)
    ? displayOutput
    : null;
}

export function toolDiffIsTruncated(tool: SessionTool): boolean {
  return isEditTool(tool) && editDiffIsTruncated(tool.input, tool.edit);
}

// Tiering follows the tool kind. A long Read result is still an inline Read;
// a Bash result is still a block even when it has one short line.
export function toolPresentation(tool: SessionTool, displayOutput = ""): ToolPresentation {
  if (toolDiffSource(tool, displayOutput) || toolDiffIsTruncated(tool)) return "diff";
  if (isBashTool(tool) || isRichWriteOrTaskTool(tool)) return "block";
  return "inline";
}

export function toolInlineResult(tool: SessionTool, displayOutput: string): string | null {
  const value = displayOutput.replace(/\s+/g, " ").trim();
  if (!value) return null;
  const count = displayOutput.split("\n").filter((line) => line.trim()).length;
  if (isReadOrSearchTool(tool)) {
    const noun = tool.archetype === "read" || ["read", "notebookread"].includes(toolName(tool))
      ? count === 1 ? "line" : "lines"
      : count === 1 ? "match" : "matches";
    return `(${count} ${noun})`;
  }
  return value.length > 120 ? `${value.slice(0, 117)}...` : value;
}

export function toolStatus(tool: SessionTool): "working" | "done" | "failed" | "completed" {
  if (tool.output === null && tool.ok === null) return "working";
  if (tool.ok === false) return "failed";
  if (tool.ok === true) return "done";
  return "completed";
}

export function outputLabelForTool(tool: SessionTool): string {
  if (tool.ok === false) return "error output";
  if (tool.archetype === "read") return "file contents";
  if (tool.archetype === "edit") return "diff";
  if (/\bdiff\b/i.test(tool.name) || /\bdiff\b/i.test(tool.summary)) {
    return "diff";
  }
  if (tool.name === "Bash" || tool.archetype === "bash" || tool.archetype === "terminal") return "log";
  return "tool output";
}

export function traceRows(timeline: Array<{ event: SessionEvent; eventIndex: number; kind: "event" | "result" }>): TraceRow[] {
  const rows: TraceRow[] = [];
  const seenTools = new Set<number>();
  for (const item of timeline) {
    if (item.kind === "result") continue;
    if (item.event.kind === "tool" && item.event.tool) {
      if (seenTools.has(item.event.id)) continue;
      seenTools.add(item.event.id);
      rows.push({
        kind: "tool",
        event: item.event,
        eventIndex: item.eventIndex,
        withResult: item.event.tool.output !== null || item.event.tool.ok !== null,
      });
      continue;
    }
    if (item.event.kind === "thinking" && item.event.text) {
      rows.push({ kind: "thinking", event: item.event, eventIndex: item.eventIndex });
    }
  }
  return rows;
}
