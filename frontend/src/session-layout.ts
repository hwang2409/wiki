import type { SessionEvent } from "./api";

export const VIRTUAL_ROW_GAP = 14;

export type EventRow = {
  event: SessionEvent;
  key: number;
  live?: boolean;
  thoughts?: readonly EventRow[];
};

export type RowPresentation = "inline" | "block" | "thought" | "prose";

const GITHUB_PREVIEW_URL = /https:\/\/(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/(?:pull\/\d+|issues\/\d+|commit\/[0-9a-fA-F]{7,40})\/?/;

function blockTool(tool: NonNullable<SessionEvent["tool"]>): boolean {
  const name = tool.name.trim().toLowerCase();
  return tool.archetype === "bash"
    || tool.archetype === "terminal"
    || tool.archetype === "diff"
    || tool.archetype === "edit"
    || tool.archetype === "agent"
    || ["bash", "edit", "multiedit", "notebookedit", "apply_patch", "monitor", "write", "writefile", "task", "agent"].includes(name)
    || GITHUB_PREVIEW_URL.test(tool.output ?? "");
}

export type RowMeasurement = {
  refs: readonly SessionEvent[];
  height: number;
};

export type VirtualLayout = {
  keys: number[];
  keyToIndex: Map<number, number>;
  tops: number[];
  sizes: number[];
  totalHeight: number;
};

export type EventRowsCache = {
  events: SessionEvent[];
  rows: EventRow[];
  offset: number;
};

export type EventRowsResult = {
  cache: EventRowsCache;
  changedFrom: number;
  rows: EventRow[];
};

export type VirtualLayoutCache = {
  rows: EventRow[];
  heightsVersion: number;
  layout: VirtualLayout;
};

function firstChangedRef<T>(previous: T[], next: T[]): number {
  if (previous === next) return next.length;
  if (
    next.length > previous.length &&
    previous.length > 0 &&
    previous[previous.length - 1] === next[previous.length - 1]
  ) {
    return previous.length;
  }
  const common = Math.min(previous.length, next.length);
  let index = 0;
  while (index < common && previous[index] === next[index]) index += 1;
  return index;
}

// WIKI-259: events that render no visible content must not become virtual
// rows. An invisible row still reserved its estimated/minimum height plus the
// inter-row gap, which painted as a random blank band between adjacent rows.
// Mirrors the render layer: traceRows drops text-less thinking events and
// payload-less tool events; an empty assistant body renders an empty div.
export function eventRendersRow(event: SessionEvent): boolean {
  if (event.kind === "claude_init") return false;
  if (event.kind === "thinking") return Boolean(event.text);
  if (event.kind === "tool") return Boolean(event.tool);
  if (event.kind === "assistant") return event.text.trim().length > 0;
  return true;
}

function appendRows(rows: EventRow[], events: SessionEvent[], offset: number, start: number): void {
  for (let index = start; index < events.length; index += 1) {
    if (!eventRendersRow(events[index])) continue;
    rows.push({ event: events[index], key: offset + index });
  }
}

function firstAffectedRow(rows: EventRow[], offset: number, eventIndex: number): number {
  const absoluteIndex = offset + eventIndex;
  let low = 0;
  let high = rows.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (rows[middle].key < absoluteIndex) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function eventRows(events: SessionEvent[], offset: number): EventRow[] {
  const rows: EventRow[] = [];
  appendRows(rows, events, offset, 0);
  return rows;
}

export function eventRowsIncremental(
  events: SessionEvent[],
  offset: number,
  previous: EventRowsCache | null,
  changedEventHint?: number,
): EventRowsResult {
  if (!previous || previous.offset !== offset) {
    const rows = eventRows(events, offset);
    return { cache: { events, rows, offset }, changedFrom: 0, rows };
  }
  if (previous.events === events) {
    return { cache: previous, changedFrom: previous.rows.length, rows: previous.rows };
  }

  const changedEvent = Math.min(
    events.length,
    changedEventHint === undefined
      ? firstChangedRef(previous.events, events)
      : Math.max(0, changedEventHint),
  );
  if (changedEvent === events.length && events.length === previous.events.length) {
    const cache = { ...previous, events };
    return { cache, changedFrom: previous.rows.length, rows: previous.rows };
  }

  const changedRow = firstAffectedRow(previous.rows, offset, changedEvent);
  // Rebuild from the changed EVENT, not from the next kept row's key: a
  // hidden event (no virtual row yet) can become visible when its ref
  // changes, e.g. a thinking block whose text just streamed in.
  const rows = previous.rows.slice(0, changedRow);
  appendRows(rows, events, offset, changedEvent);
  return {
    cache: { events, rows, offset },
    changedFrom: changedRow,
    rows,
  };
}

export function sameEventRefs(prev: readonly SessionEvent[], next: readonly SessionEvent[]): boolean {
  return prev.length === next.length && prev.every((event, index) => event === next[index]);
}

export function rowEventRefs(row: EventRow): readonly SessionEvent[] {
  return row.thoughts ? row.thoughts.map((thought) => thought.event) : [row.event];
}

function estimateWrappedLines(text: string, charsPerLine: number): number {
  let total = 0;
  for (const line of text.split("\n")) total += Math.max(1, Math.ceil(line.length / charsPerLine));
  return total;
}

export function rowPresentation(row: EventRow): RowPresentation {
  if (row.event.kind === "thinking") return "thought";
  if (row.event.kind === "tool" && row.event.tool) {
    return blockTool(row.event.tool) ? "block" : "inline";
  }
  if (row.event.kind === "bash") return "block";
  return "prose";
}

function getEstimatedRowHeight(row: EventRow): number {
  switch (row.event.kind) {
    case "assistant":
      return Math.max(96, 28 + estimateWrappedLines(row.event.text, 92) * 22);
    case "bash":
      return Math.max(
        76,
        28 +
          estimateWrappedLines(row.event.bash?.input ?? "", 92) * 20 +
          estimateWrappedLines(
            [row.event.bash?.stdout, row.event.bash?.stderr].filter(Boolean).join("\n"),
            104,
          ) * 18,
      );
    case "tasks":
      return Math.max(72, 40 + (row.event.tasks?.length ?? 0) * 28);
    case "question":
      return Math.max(132, 56 + (row.event.question?.options.length ?? 0) * 28);
    case "terminal":
      return Math.max(60, 24 + estimateWrappedLines(row.event.text, 112) * 18);
    case "user":
      return Math.max(52, 20 + estimateWrappedLines(row.event.text, 96) * 20);
    case "image":
      return 44;
    case "artifact":
      return 420;
    case "notification":
    case "command":
    case "interrupt":
    case "pr":
    case "marker":
      return Math.max(40, 18 + estimateWrappedLines(row.event.text, 92) * 18);
    default:
      return 56;
  }
}

function getRowHeight(row: EventRow, heights: Map<number, RowMeasurement>): number {
  const measurement = heights.get(row.key);
  const refs = rowEventRefs(row);
  return measurement && sameEventRefs(measurement.refs, refs)
    ? measurement.height
    : getEstimatedRowHeight(row);
}

export function buildVirtualLayout(
  rows: EventRow[],
  heights: Map<number, RowMeasurement>,
): VirtualLayout {
  return buildVirtualLayoutIncremental(rows, heights, 0, null).layout;
}

export function buildVirtualLayoutIncremental(
  rows: EventRow[],
  heights: Map<number, RowMeasurement>,
  heightsVersion: number,
  previous: VirtualLayoutCache | null,
  changedGroupHint?: number,
): { cache: VirtualLayoutCache; layout: VirtualLayout } {
  let changedFrom = previous ? firstChangedRef(previous.rows, rows) : 0;
  if (changedGroupHint !== undefined) changedFrom = Math.min(changedFrom, Math.max(0, changedGroupHint));
  if (previous && previous.heightsVersion !== heightsVersion && changedGroupHint === undefined) changedFrom = 0;
  if (previous && previous.rows === rows && previous.heightsVersion === heightsVersion) {
    return { cache: previous, layout: previous.layout };
  }
  if (
    previous &&
    rows.length !== previous.rows.length &&
    changedFrom === Math.min(rows.length, previous.rows.length)
  ) {
    changedFrom = Math.max(0, changedFrom - 1);
  }
  // A row's presentation controls the gap before it. Rebuild the preceding
  // row when a changed row can alter that boundary.
  if (previous && changedFrom > 0) changedFrom -= 1;

  const keys = previous?.layout.keys.slice(0, changedFrom) ?? [];
  const tops = previous?.layout.tops.slice(0, changedFrom) ?? [];
  const sizes = previous?.layout.sizes.slice(0, changedFrom) ?? [];
  // Rebuild this index for correctness after trims/reorders. This remains
  // O(N), but it only stores numeric key/index pairs; height estimation and
  // cumulative-top work below stay O(delta), which is the expensive path.
  const keyToIndex = new Map<number, number>();
  for (let index = 0; index < changedFrom; index += 1) keyToIndex.set(keys[index], index);
  let offset = changedFrom > 0 ? tops[changedFrom - 1] + sizes[changedFrom - 1] : 0;
  for (let index = changedFrom; index < rows.length; index += 1) {
    const row = rows[index];
    keys[index] = row.key;
    keyToIndex.set(row.key, index);
    tops[index] = offset;
    const height = getRowHeight(row, heights);
    const next = rows[index + 1];
    const adjacentTierOneInline = next
      && rowPresentation(row) === "inline"
      && rowPresentation(next) === "inline";
    const size = height + (adjacentTierOneInline || index === rows.length - 1 ? 0 : VIRTUAL_ROW_GAP);
    sizes[index] = size;
    offset += size;
  }
  const layout = { keys, keyToIndex, tops, sizes, totalHeight: offset };
  const cache = { rows, heightsVersion, layout };
  return { cache, layout };
}
