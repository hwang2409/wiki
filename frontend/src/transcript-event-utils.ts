import type { SessionEvent, SessionTool } from "./api";

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

export function toolStatus(tool: SessionTool): "working" | "done" | "failed" | "completed" {
  if (tool.output === null && tool.ok === null) return "working";
  if (tool.ok === false) return "failed";
  if (tool.ok === true) return "done";
  return "completed";
}

export function outputLabelForTool(tool: SessionTool): string {
  if (tool.ok === false) return "error output";
  if (tool.archetype === "read") return "file contents";
  if (tool.archetype === "edit") return "acknowledgement";
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

export function traceConnector(rows: TraceRow[], index: number): string {
  const next = rows[index + 1];
  return next && next.kind !== "thinking" ? "├" : "└";
}
