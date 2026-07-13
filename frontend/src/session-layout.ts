import type { SessionEvent } from "./api";

export const VIRTUAL_ROW_GAP = 14;

export type EventGroup =
  | { kind: "message"; event: SessionEvent; key: number }
  | { kind: "activity"; events: SessionEvent[]; key: number };

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

export type GroupEventsCache = {
  events: SessionEvent[];
  groups: EventGroup[];
  offset: number;
};

export type GroupEventsResult = {
  cache: GroupEventsCache;
  changedFrom: number;
  groups: EventGroup[];
};

export type VirtualLayoutCache = {
  groups: EventGroup[];
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

function appendGroups(groups: EventGroup[], events: SessionEvent[], offset: number, start: number): void {
  for (let index = start; index < events.length; index += 1) {
    const event = events[index];
    if (event.kind !== "tool" && event.kind !== "thinking") {
      groups.push({ kind: "message", event, key: offset + index });
      continue;
    }
    const last = groups[groups.length - 1];
    if (last?.kind === "activity") last.events.push(event);
    else groups.push({ kind: "activity", events: [event], key: offset + index });
  }
}

function firstAffectedGroup(groups: EventGroup[], offset: number, eventIndex: number): number {
  const absoluteIndex = offset + eventIndex;
  let low = 0;
  let high = groups.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (groups[middle].key < absoluteIndex) low = middle + 1;
    else high = middle;
  }
  if (low > 0) {
    const previous = groups[low - 1];
    const length = previous.kind === "activity" ? previous.events.length : 1;
    if (previous.key + length > absoluteIndex) return low - 1;
  }
  return low;
}

export function groupEvents(events: SessionEvent[], offset: number): EventGroup[] {
  const groups: EventGroup[] = [];
  appendGroups(groups, events, offset, 0);
  return groups;
}

export function groupEventsIncremental(
  events: SessionEvent[],
  offset: number,
  previous: GroupEventsCache | null,
  changedEventHint?: number,
): GroupEventsResult {
  if (!previous || previous.offset !== offset) {
    const groups = groupEvents(events, offset);
    return { cache: { events, groups, offset }, changedFrom: 0, groups };
  }
  if (previous.events === events) {
    return { cache: previous, changedFrom: previous.groups.length, groups: previous.groups };
  }

  const changedEvent = Math.min(
    events.length,
    changedEventHint === undefined
      ? firstChangedRef(previous.events, events)
      : Math.max(0, changedEventHint),
  );
  if (changedEvent === events.length && events.length === previous.events.length) {
    const cache = { ...previous, events };
    return { cache, changedFrom: previous.groups.length, groups: previous.groups };
  }

  let changedGroup = firstAffectedGroup(previous.groups, offset, changedEvent);
  let rebuildEvent = changedEvent;
  const priorGroup = previous.groups[changedGroup - 1];
  const nextEvent = events[changedEvent];
  if (priorGroup?.kind === "activity" && nextEvent && (nextEvent.kind === "tool" || nextEvent.kind === "thinking")) {
    changedGroup -= 1;
  }
  if (changedGroup < previous.groups.length) {
    rebuildEvent = previous.groups[changedGroup].key - offset;
  }

  const groups = previous.groups.slice(0, changedGroup);
  appendGroups(groups, events, offset, rebuildEvent);
  return {
    cache: { events, groups, offset },
    changedFrom: changedGroup,
    groups,
  };
}

export function sameEventRefs(prev: readonly SessionEvent[], next: readonly SessionEvent[]): boolean {
  return prev.length === next.length && prev.every((event, index) => event === next[index]);
}

function estimateWrappedLines(text: string, charsPerLine: number): number {
  let total = 0;
  for (const line of text.split("\n")) total += Math.max(1, Math.ceil(line.length / charsPerLine));
  return total;
}

function getEstimatedGroupHeight(group: EventGroup): number {
  if (group.kind === "activity") return 34;
  switch (group.event.kind) {
    case "assistant":
      return Math.max(96, 28 + estimateWrappedLines(group.event.text, 92) * 22);
    case "bash":
      return Math.max(
        76,
        28 +
          estimateWrappedLines(group.event.bash?.input ?? "", 92) * 20 +
          estimateWrappedLines(
            [group.event.bash?.stdout, group.event.bash?.stderr].filter(Boolean).join("\n"),
            104,
          ) * 18,
      );
    case "tasks":
      return Math.max(72, 40 + (group.event.tasks?.length ?? 0) * 28);
    case "question":
      return Math.max(132, 56 + (group.event.question?.options.length ?? 0) * 28);
    case "terminal":
      return Math.max(60, 24 + estimateWrappedLines(group.event.text, 112) * 18);
    case "user":
      return Math.max(52, 20 + estimateWrappedLines(group.event.text, 96) * 20);
    case "image":
      return 44;
    case "artifact":
      return 420;
    case "notification":
    case "command":
    case "interrupt":
    case "pr":
    case "marker":
      return Math.max(40, 18 + estimateWrappedLines(group.event.text, 92) * 18);
    default:
      return 56;
  }
}

function getGroupHeight(group: EventGroup, heights: Map<number, RowMeasurement>): number {
  const measurement = heights.get(group.key);
  const refs = group.kind === "activity" ? group.events : [group.event];
  return measurement && sameEventRefs(measurement.refs, refs)
    ? measurement.height
    : getEstimatedGroupHeight(group);
}

export function buildVirtualLayout(
  groups: EventGroup[],
  heights: Map<number, RowMeasurement>,
): VirtualLayout {
  return buildVirtualLayoutIncremental(groups, heights, 0, null).layout;
}

export function buildVirtualLayoutIncremental(
  groups: EventGroup[],
  heights: Map<number, RowMeasurement>,
  heightsVersion: number,
  previous: VirtualLayoutCache | null,
  changedGroupHint?: number,
): { cache: VirtualLayoutCache; layout: VirtualLayout } {
  let changedFrom = previous ? firstChangedRef(previous.groups, groups) : 0;
  if (changedGroupHint !== undefined) changedFrom = Math.min(changedFrom, Math.max(0, changedGroupHint));
  if (previous && previous.heightsVersion !== heightsVersion && changedGroupHint === undefined) changedFrom = 0;
  if (previous && previous.groups === groups && previous.heightsVersion === heightsVersion) {
    return { cache: previous, layout: previous.layout };
  }
  if (
    previous &&
    groups.length !== previous.groups.length &&
    changedFrom === Math.min(groups.length, previous.groups.length)
  ) {
    changedFrom = Math.max(0, changedFrom - 1);
  }

  const keys = previous?.layout.keys.slice(0, changedFrom) ?? [];
  const tops = previous?.layout.tops.slice(0, changedFrom) ?? [];
  const sizes = previous?.layout.sizes.slice(0, changedFrom) ?? [];
  // Rebuild this index for correctness after trims/reorders. This remains
  // O(N), but it only stores numeric key/index pairs; height estimation and
  // cumulative-top work below stay O(delta), which is the expensive path.
  const keyToIndex = new Map<number, number>();
  for (let index = 0; index < changedFrom; index += 1) keyToIndex.set(keys[index], index);
  let offset = changedFrom > 0 ? tops[changedFrom - 1] + sizes[changedFrom - 1] : 0;
  for (let index = changedFrom; index < groups.length; index += 1) {
    const group = groups[index];
    keys[index] = group.key;
    keyToIndex.set(group.key, index);
    tops[index] = offset;
    const height = getGroupHeight(group, heights);
    const size = height + (index === groups.length - 1 ? 0 : VIRTUAL_ROW_GAP);
    sizes[index] = size;
    offset += size;
  }
  const layout = { keys, keyToIndex, tops, sizes, totalHeight: offset };
  const cache = { groups, heightsVersion, layout };
  return { cache, layout };
}
