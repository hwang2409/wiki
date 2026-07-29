import { useMemo, useState } from "react";
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

function hookKey(event: ProviderStreamEvent): string | null {
  const run = eventRun(event);
  const name = stringValue(run.eventName) ?? stringValue(run.name) ?? stringValue(run.id);
  const turnId = stringValue(eventParams(event).turnId);
  return name && turnId ? `${name}\u0000${turnId}` : null;
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
      const key = hookKey(event);
      if (!key) continue;
      const queue = starts.get(key) ?? [];
      queue.push(event);
      starts.set(key, queue);
      continue;
    }
    if (event.kind !== "hook_completed") continue;
    const key = hookKey(event);
    if (!key) continue;
    const start = starts.get(key)?.shift();
    const run = eventRun(event);
    const explicitDuration = run.durationMs;
    const durationMs = typeof explicitDuration === "number"
      ? Math.max(0, explicitDuration)
      : start
        ? Math.max(0, Date.parse(event.normalized_at) - Date.parse(start.normalized_at))
        : null;
    const name = stringValue(run.eventName) ?? stringValue(run.name) ?? key.split("\u0000")[0];
    chips.push({ key: `${key}\u0000${event.seq}`, name, durationMs, seq: event.seq });
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

function commandItemIds(events: ProviderStreamEvent[]): Set<string> {
  const ids = new Set<string>();
  for (const event of events) {
    if (event.kind !== "item_started" && event.kind !== "item_completed") continue;
    const item = recordValue(eventParams(event).item);
    const type = stringValue(item?.type);
    if (type === "commandExecution") {
      const id = commandItemId(event);
      if (id) ids.add(id);
    }
  }
  return ids;
}

export type TerminalInteraction = {
  event: ProviderStreamEvent;
  itemId: string;
  stdin: string;
};

export function matchedTerminalInteractions(events: ProviderStreamEvent[]): TerminalInteraction[] {
  const ids = commandItemIds(events);
  return events
    .filter((event) => event.kind === "item_commandExecution_terminalInteraction")
    .map((event) => {
      const itemId = commandItemId(event);
      const stdin = stringValue(eventParams(event).stdin);
      return itemId && stdin ? { event, itemId, stdin } : null;
    })
    .filter((interaction): interaction is TerminalInteraction => Boolean(interaction && ids.has(interaction.itemId)));
}

function diffPath(file: DiffFilePatch): string {
  return fileTitle(file);
}

export function applyDiffSnapshot(
  previous: ReadonlyMap<string, DiffFilePatch>,
  source: string,
): Map<string, DiffFilePatch> {
  const next = new Map(previous);
  for (const file of parseUnifiedDiff(source)) next.set(diffPath(file), file);
  return next;
}

function diffSnapshots(events: ProviderStreamEvent[]): Map<string, DiffFilePatch> {
  let files = new Map<string, DiffFilePatch>();
  for (const event of events) {
    if (event.kind !== "turn_diff_updated") continue;
    const diff = stringValue(eventParams(event).diff);
    if (diff) files = applyDiffSnapshot(files, diff);
  }
  return files;
}

function WarningRenderer({ event, moderation = false }: { event: ProviderStreamEvent; moderation?: boolean }) {
  const params = eventParams(event);
  const flags = Array.isArray(params.flags) ? params.flags : params.flag ? [params.flag] : [];
  const details = flags
    .map((flag) => {
      const value = recordValue(flag);
      return stringValue(value?.name) ?? stringValue(value?.flag) ?? stringValue(flag);
    })
    .filter((flag): flag is string => Boolean(flag && flag.toLowerCase() !== "safe"));
  const message = stringValue(params.message) ?? (details.join(", ") || "moderation flag raised");
  return (
    <div className={`codex-stream-warning${moderation ? " is-moderation" : ""}`} role="status">
      <AlertTriangle aria-hidden="true" size={13} />
      <span className="codex-stream-warning-label">{moderation ? "moderation" : "warning"}</span>
      {details.length ? <span className="codex-stream-warning-flag">{details.join(", ")}</span> : null}
      <span className="codex-stream-warning-message">{message}</span>
    </div>
  );
}

function DiffRenderer({ events }: { events: ProviderStreamEvent[] }) {
  const files = useMemo(() => diffSnapshots(events), [events]);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  if (!files.size) return null;
  return (
    <div className="codex-stream-artifact codex-stream-diff" data-testid="codex-diff-renderer">
      <div className="codex-stream-artifact-head">
        <span>working diff</span>
        <span className="codex-stream-artifact-count tabular-nums">{files.size} file{files.size === 1 ? "" : "s"}</span>
      </div>
      {[...files.entries()].map(([path, file]) => {
        const isCollapsed = collapsed.has(path);
        const kind = fileKind(file);
        return (
          <div className="codex-stream-diff-file" key={path}>
            <button
              className="codex-stream-diff-file-head"
              type="button"
              onClick={() => setCollapsed((current) => {
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
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function TerminalInteractionRenderer({ events }: { events: ProviderStreamEvent[] }) {
  const interactions = matchedTerminalInteractions(events);
  if (!interactions.length) return null;
  return (
    <div className="codex-stream-command-cards" data-testid="codex-terminal-interactions">
      {interactions.map(({ event, itemId, stdin }) => (
        <div className="codex-stream-command-card" key={event.seq}>
          <Terminal aria-hidden="true" size={13} />
          <span className="codex-stream-command-label">stdin</span>
          <code>{stdin}</code>
          <span className="codex-stream-command-id">{itemId}</span>
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
      {diff.length ? <code>{diff.join(", ")}</code> : null}
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

export function CodexStreamHighlights({ events }: { events: ProviderStreamEvent[] }) {
  const rendered = events.filter((event) => event.disposition === "rendered");
  const warningEvents = rendered.filter((event) => event.kind === "warning");
  const moderationEvents = rendered.filter((event) => event.kind === "turn_moderationMetadata_warning");
  const skillEvents = rendered.filter((event) => event.kind === "skills_changed");
  const planEvents = rendered.filter((event) => event.kind === "turn_plan_updated");
  if (!rendered.length) return null;
  return (
    <div className="codex-stream-highlights">
      {warningEvents.map((event) => <WarningRenderer event={event} key={event.seq} />)}
      {moderationEvents.map((event) => <WarningRenderer event={event} key={event.seq} moderation />)}
      <HookLifecycleRenderer events={events} />
      <TerminalInteractionRenderer events={events} />
      <DiffRenderer events={events} />
      {skillEvents.map((event) => <SkillsChangedRenderer event={event} key={event.seq} />)}
      {planEvents.map((event) => <PlanRenderer event={event} key={event.seq} />)}
    </div>
  );
}
