import { useEffect, useMemo, useState } from "react";
import { AlertTriangle, ChevronRight, ListTodo, Terminal } from "lucide-react";
import type { ProviderStreamEvent } from "./api";
import {
  fileKind,
  fileKindLabel,
  fileTitle,
  parseUnifiedDiff,
  type DiffFilePatch,
} from "./diff-parser";

type RecordValue = Record<string, unknown>;

function recordValue(value: unknown): RecordValue | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as RecordValue
    : null;
}

function eventParams(event: ProviderStreamEvent): RecordValue {
  return recordValue(event.payload.params) ?? {};
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function eventRun(event: ProviderStreamEvent): RecordValue {
  return recordValue(eventParams(event).run) ?? {};
}

function hookKeys(event: ProviderStreamEvent): string[] {
  const run = eventRun(event);
  const keys: string[] = [];
  const runId = stringValue(run.id);
  if (runId) keys.push(`run:${runId}`);
  const name = stringValue(run.eventName) ?? stringValue(run.name) ?? stringValue(run.id);
  const turnId = stringValue(eventParams(event).turnId);
  if (name && turnId) keys.push(`${name}\u0000${turnId}`);
  return keys;
}

function hookDisplayKey(event: ProviderStreamEvent): string | null {
  const run = eventRun(event);
  const name = stringValue(run.eventName) ?? stringValue(run.name) ?? stringValue(run.id);
  const turnId = stringValue(eventParams(event).turnId);
  if (name && turnId) return `${name}\u0000${turnId}`;
  const runId = stringValue(run.id);
  return runId ? `run:${runId}` : name ? `name:${name}` : null;
}

export type HookChip = {
  key: string;
  name: string;
  durationMs: number | null;
  seq: number;
};

export function deriveHookChips(events: ProviderStreamEvent[]): HookChip[] {
  const starts = new Map<string, ProviderStreamEvent[]>();
  const chips: HookChip[] = [];
  for (const event of events) {
    if (event.kind === "hook_started") {
      for (const key of hookKeys(event)) {
        const queue = starts.get(key) ?? [];
        queue.push(event);
        starts.set(key, queue);
      }
      continue;
    }
    if (event.kind !== "hook_completed") continue;
    let start: ProviderStreamEvent | undefined;
    for (const key of hookKeys(event)) {
      const candidate = starts.get(key)?.shift();
      if (candidate) {
        start = candidate;
        break;
      }
    }
    const run = eventRun(event);
    const explicitDuration = run.durationMs;
    const name = stringValue(run.eventName) ?? stringValue(run.name) ?? stringValue(run.id);
    if (!name || (!start && typeof explicitDuration !== "number")) continue;
    if (start) {
      for (const key of hookKeys(start)) {
        const queue = starts.get(key);
        if (!queue) continue;
        const index = queue.indexOf(start);
        if (index >= 0) queue.splice(index, 1);
      }
    }
    const durationMs = typeof explicitDuration === "number"
      ? Math.max(0, explicitDuration)
      : Math.max(0, Date.parse(event.normalized_at) - Date.parse(start!.normalized_at));
    chips.push({
      key: `${hookDisplayKey(event) ?? name}\u0000${event.seq}`,
      name,
      durationMs,
      seq: event.seq,
    });
  }
  return chips;
}

function formatDuration(durationMs: number | null): string {
  if (durationMs === null || !Number.isFinite(durationMs)) return "duration unavailable";
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  return `${(durationMs / 1000).toFixed(durationMs >= 10_000 ? 1 : 2)}s`;
}

function commandItemId(event: ProviderStreamEvent): string | null {
  const params = eventParams(event);
  const item = recordValue(params.item);
  return stringValue(params.itemId) ?? stringValue(item?.id);
}

export type TerminalInteraction = {
  event: ProviderStreamEvent;
  itemId: string;
  stdin: string;
};

export function matchedTerminalInteractions(events: ProviderStreamEvent[]): TerminalInteraction[] {
  return events
    .filter((event) => event.kind === "item_commandExecution_terminalInteraction")
    .map((event) => {
      const itemId = commandItemId(event);
      const stdin = eventParams(event).stdin;
      return itemId && typeof stdin === "string" ? { event, itemId, stdin } : null;
    })
    .filter((interaction): interaction is TerminalInteraction => Boolean(interaction));
}

export type CommandExecutionCard = {
  itemId: string;
  command: string | null;
  interactions: TerminalInteraction[];
};

export function commandExecutionCards(events: ProviderStreamEvent[]): CommandExecutionCard[] {
  const interactions = matchedTerminalInteractions(events);
  const byItem = new Map<string, TerminalInteraction[]>();
  for (const interaction of interactions) {
    const current = byItem.get(interaction.itemId) ?? [];
    current.push(interaction);
    byItem.set(interaction.itemId, current);
  }
  const cards = new Map<string, CommandExecutionCard>(
    [...byItem.entries()].map(([itemId, itemInteractions]) => [itemId, {
      itemId,
      command: null,
      interactions: itemInteractions,
    }]),
  );
  for (const event of events) {
    if (event.kind !== "item_started" && event.kind !== "item_completed") continue;
    const params = eventParams(event);
    const item = recordValue(params.item);
    if (stringValue(item?.type) !== "commandExecution") continue;
    const itemId = commandItemId(event);
    if (!itemId || !byItem.has(itemId)) continue;
    const previous = cards.get(itemId);
    cards.set(itemId, {
      itemId,
      command: stringValue(item?.command) ?? previous?.command ?? null,
      interactions: previous?.interactions ?? byItem.get(itemId) ?? [],
    });
  }
  return [...cards.values()];
}

function diffPath(file: DiffFilePatch): string {
  return fileTitle(file);
}

export function parseDiffSnapshot(source: string): Map<string, DiffFilePatch> {
  const files = new Map<string, DiffFilePatch>();
  for (const file of parseUnifiedDiff(source)) files.set(diffPath(file), file);
  return files;
}

function latestDiffSource(events: ProviderStreamEvent[]): string | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event?.kind !== "turn_diff_updated") continue;
    const diff = eventParams(event).diff;
    return typeof diff === "string" ? diff : null;
  }
  return null;
}

function WarningRenderer({ event, moderation = false }: { event: ProviderStreamEvent; moderation?: boolean }) {
  const params = eventParams(event);
  const details: string[] = [];
  const addFlag = (name: string) => {
    if (name.toLowerCase() !== "safe" && !details.includes(name)) details.push(name);
  };
  const visitFlags = (value: unknown, inFlagMap = false) => {
    if (Array.isArray(value)) {
      value.forEach((child) => visitFlags(child, inFlagMap));
      return;
    }
    const record = recordValue(value);
    if (!record) {
      if (inFlagMap && typeof value === "string") addFlag(value);
      return;
    }
    for (const [key, child] of Object.entries(record)) {
      const childIsFlagMap = inFlagMap
        || key === "flag"
        || key === "flags"
        || key === "category_flags"
        || key === "labels";
      if (childIsFlagMap && child === true) addFlag(key);
      else if (childIsFlagMap && typeof child === "string") addFlag(child);
      else if (key === "is_blocked" && child === true) addFlag("blocked");
      else if (child && typeof child === "object") visitFlags(child, childIsFlagMap);
    }
  };
  visitFlags(params);
  const message = stringValue(params.message) ?? "moderation flag raised";
  return (
    <div className={`codex-stream-warning${moderation ? " is-moderation" : ""}`} role="status">
      <AlertTriangle aria-hidden="true" size={13} />
      <span className="codex-stream-warning-label">{moderation ? "moderation" : "warning"}</span>
      {details.length ? <span className="codex-stream-warning-flag">{details.join(", ")}</span> : null}
      <span className="codex-stream-warning-message">{message}</span>
    </div>
  );
}

const LARGE_DIFF_FILE_LINES = 500;
const LARGE_DIFF_FILE_BYTES = 64 * 1024;
const MAX_DIFF_FILE_LINES = 1_000;
const MAX_DIFF_FILE_BYTES = 128 * 1024;
const MAX_DIFF_TOTAL_LINES = 2_000;
const MAX_DIFF_TOTAL_BYTES = 256 * 1024;

type BoundedDiffFile = {
  file: DiffFilePatch;
  lineCount: number;
  byteCount: number;
  truncated: boolean;
};

function textBytes(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

function diffFileStats(file: DiffFilePatch): { lineCount: number; byteCount: number } {
  let lineCount = 0;
  let byteCount = 0;
  for (const hunk of file.hunks) {
    byteCount += textBytes(hunk.header);
    for (const line of hunk.lines) {
      lineCount += 1;
      byteCount += textBytes(line.text) + 1;
    }
  }
  return { lineCount, byteCount };
}

function boundDiffFile(
  file: DiffFilePatch,
  maxLines: number,
  maxBytes: number,
): BoundedDiffFile {
  let lineCount = 0;
  let byteCount = 0;
  let truncated = false;
  const hunks = [];
  for (const hunk of file.hunks) {
    const lines = [];
    byteCount += textBytes(hunk.header);
    for (const line of hunk.lines) {
      const lineBytes = textBytes(line.text) + 1;
      if (lineCount >= maxLines || byteCount + lineBytes > maxBytes) {
        truncated = true;
        break;
      }
      lines.push(line);
      lineCount += 1;
      byteCount += lineBytes;
    }
    hunks.push({ ...hunk, lines });
    if (truncated) break;
  }
  return {
    file: { ...file, hunks },
    lineCount,
    byteCount,
    truncated,
  };
}

function boundDiffFiles(files: ReadonlyMap<string, DiffFilePatch>): Map<string, BoundedDiffFile> {
  let remainingLines = MAX_DIFF_TOTAL_LINES;
  let remainingBytes = MAX_DIFF_TOTAL_BYTES;
  const bounded = new Map<string, BoundedDiffFile>();
  for (const [path, file] of files) {
    const limited = boundDiffFile(
      file,
      Math.min(MAX_DIFF_FILE_LINES, remainingLines),
      Math.min(MAX_DIFF_FILE_BYTES, remainingBytes),
    );
    bounded.set(path, limited);
    remainingLines -= limited.lineCount;
    remainingBytes -= limited.byteCount;
  }
  return bounded;
}

function DiffRenderer({ source }: { source: string | null }) {
  const boundedFiles = useMemo(
    () => (source === null
      ? new Map<string, BoundedDiffFile>()
      : boundDiffFiles(parseDiffSnapshot(source))),
    [source],
  );
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [expandedLarge, setExpandedLarge] = useState<Set<string>>(new Set());
  useEffect(() => {
    setCollapsed(new Set());
    setExpandedLarge(new Set());
  }, [source]);
  if (!boundedFiles.size) return null;
  return (
    <div className="codex-stream-artifact codex-stream-diff" data-testid="codex-diff-renderer">
      <div className="codex-stream-artifact-head">
        <span>working diff</span>
        <span className="codex-stream-artifact-count tabular-nums">{boundedFiles.size} file{boundedFiles.size === 1 ? "" : "s"}</span>
      </div>
      {[...boundedFiles.entries()].map(([path, bounded]) => {
        const { file } = bounded;
        const stats = diffFileStats(file);
        const isLarge = stats.lineCount > LARGE_DIFF_FILE_LINES || stats.byteCount > LARGE_DIFF_FILE_BYTES;
        const isCollapsed = isLarge ? !expandedLarge.has(path) : collapsed.has(path);
        const kind = fileKind(file);
        return (
          <div className="codex-stream-diff-file" key={path}>
            <button
              className="codex-stream-diff-file-head"
              type="button"
              onClick={() => setCollapsed((current) => {
                if (isLarge) {
                  setExpandedLarge((expanded) => {
                    const next = new Set(expanded);
                    if (next.has(path)) next.delete(path); else next.add(path);
                    return next;
                  });
                  return current;
                }
                const next = new Set(current);
                if (next.has(path)) next.delete(path); else next.add(path);
                return next;
              })}
            >
              <ChevronRight className={isCollapsed ? "" : "is-open"} size={12} />
              <span>{path}</span>
              {kind ? <span className="codex-stream-diff-kind">{fileKindLabel(kind)}</span> : null}
            </button>
            {!isCollapsed ? (
              <div className="codex-stream-diff-body">
                {file.hunks.map((hunk) => (
                  <div className="codex-stream-diff-hunk" key={`${path}:${hunk.header}`}>
                    <div className="codex-stream-diff-hunk-head">{hunk.header}</div>
                    {hunk.lines.map((line, index) => (
                      <div className={`codex-stream-diff-line is-${line.kind}`} key={`${hunk.header}:${index}`}>
                        <span className="codex-stream-diff-marker">{line.kind === "add" ? "+" : line.kind === "remove" ? "-" : " "}</span>
                        <code>{line.text}</code>
                      </div>
                    ))}
                  </div>
                ))}
                {bounded.truncated ? <div className="codex-stream-diff-truncated">diff preview truncated</div> : null}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function TerminalInteractionRenderer({ events }: { events: ProviderStreamEvent[] }) {
  const cards = commandExecutionCards(events);
  if (!cards.length) return null;
  return (
    <div className="codex-stream-command-cards" data-testid="codex-terminal-interactions">
      {cards.map((card) => (
        <div className="codex-stream-command-card" key={card.itemId}>
          <Terminal aria-hidden="true" size={13} />
          <span className="codex-stream-command-label">command</span>
          <code>{card.command ?? card.itemId}</code>
          <span className="codex-stream-command-stdin-badge">
            stdin{card.interactions.length > 1 ? ` ×${card.interactions.length}` : ""}
          </span>
          {card.interactions.map((interaction) => (
            <code className="codex-stream-command-input" key={interaction.event.seq}>
              {interaction.stdin || "empty stdin"}
            </code>
          ))}
        </div>
      ))}
    </div>
  );
}

function HookLifecycleRenderer({ events }: { events: ProviderStreamEvent[] }) {
  const hooks = deriveHookChips(events);
  if (!hooks.length) return null;
  return (
    <div className="codex-stream-chip-row" data-testid="codex-hook-chips">
      {hooks.map((hook) => (
        <span className="codex-stream-chip" key={hook.key}>
          <span>hook: {hook.name}</span>
          <span className="tabular-nums">({formatDuration(hook.durationMs)})</span>
        </span>
      ))}
    </div>
  );
}

function SkillsChangedRenderer({ event }: { event: ProviderStreamEvent }) {
  const params = eventParams(event);
  const changed = recordValue(params.skills) ?? params;
  const addedValue = changed.added ?? changed.addedSkills;
  const removedValue = changed.removed ?? changed.removedSkills;
  const added = Array.isArray(addedValue) ? addedValue.filter((value): value is string => typeof value === "string") : [];
  const removed = Array.isArray(removedValue) ? removedValue.filter((value): value is string => typeof value === "string") : [];
  const diff = [...added.map((skill) => `+ ${skill}`), ...removed.map((skill) => `- ${skill}`)];
  return (
    <span className="codex-stream-chip codex-stream-skills-chip">
      <span>skills updated</span>
      <code>{diff.length ? diff.join(", ") : "list refreshed"}</code>
    </span>
  );
}

function PlanRenderer({ event }: { event: ProviderStreamEvent }) {
  const rawPlan = eventParams(event).plan;
  const plan: unknown[] = Array.isArray(rawPlan) ? rawPlan : [];
  if (!plan.length) return null;
  return (
    <div className="codex-stream-plan" data-testid="codex-plan-renderer">
      <div className="codex-stream-artifact-head"><span>plan</span><ListTodo aria-hidden="true" size={13} /></div>
      <ol>
        {plan.map((item, index) => {
          const step = recordValue(item);
          const text = stringValue(step?.step) ?? stringValue(step?.text) ?? JSON.stringify(item);
          const status = stringValue(step?.status)?.toLowerCase() ?? "pending";
          return <li className={`is-${status}`} key={`${event.seq}:${index}`}><span>{text}</span><small>{status}</small></li>;
        })}
      </ol>
    </div>
  );
}

export function CodexStreamHighlights({
  events,
  currentTurnDiff,
}: {
  events: ProviderStreamEvent[];
  currentTurnDiff?: string | null;
}) {
  const rendered = events.filter((event) => event.disposition === "rendered");
  const warningEvents = rendered.filter((event) => event.kind === "warning");
  const moderationEvents = rendered.filter((event) => event.kind === "turn_moderationMetadata_warning");
  const skillEvents = rendered.filter((event) => event.kind === "skills_changed");
  const planEvents = rendered.filter((event) => event.kind === "turn_plan_updated");
  const hasHookEvents = events.some((event) => event.kind === "hook_started" || event.kind === "hook_completed");
  if (!rendered.length && !hasHookEvents) return null;
  return (
    <div className="codex-stream-highlights">
      {warningEvents.map((event) => <WarningRenderer event={event} key={event.seq} />)}
      {moderationEvents.map((event) => <WarningRenderer event={event} key={event.seq} moderation />)}
      <HookLifecycleRenderer events={events} />
      <TerminalInteractionRenderer events={events} />
      <DiffRenderer source={currentTurnDiff === undefined ? latestDiffSource(events) : currentTurnDiff} />
      {skillEvents.map((event) => <SkillsChangedRenderer event={event} key={event.seq} />)}
      {planEvents.map((event) => <PlanRenderer event={event} key={event.seq} />)}
    </div>
  );
}
