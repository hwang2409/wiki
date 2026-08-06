import type { SessionEvent, SessionTool } from "./api";
import { editDiffFromInput, editDiffIsTruncated } from "./transcript-output";

const UNIFIED_DIFF_HEAD = /^\s*(?:diff --git |--- [ab]?\/|\*\*\* )/m;
const GITHUB_PREVIEW_URL = /https:\/\/(?:www\.)?github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/(?:pull\/\d+|issues\/\d+|commit\/[0-9a-fA-F]{7,40})\/?/;

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
  return ["monitor", "write", "writefile", "task", "agent"].includes(toolName(tool))
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
  if (GITHUB_PREVIEW_URL.test(displayOutput)) return "block";
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

// OpenCode Locale.duration port (packages/tui/src/util/locale.ts:39) — the
// transcript's single duration vocabulary: 202ms, 1.1s, 1m 20s, 2h 5m, 1d 2h.
export function formatEventDuration(durationMs: number): string {
  const input = Math.max(0, Math.round(durationMs));
  if (input < 1000) return `${input}ms`;
  if (input < 60_000) return `${(input / 1000).toFixed(1)}s`;
  if (input < 3_600_000) {
    const minutes = Math.floor(input / 60_000);
    const seconds = Math.floor((input % 60_000) / 1000);
    return `${minutes}m ${seconds}s`;
  }
  if (input < 86_400_000) {
    const hours = Math.floor(input / 3_600_000);
    const minutes = Math.floor((input % 3_600_000) / 60_000);
    return `${hours}h ${minutes}m`;
  }
  const days = Math.floor(input / 86_400_000);
  const hours = Math.floor((input % 86_400_000) / 3_600_000);
  return `${days}d ${hours}h`;
}

function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null;
  const parsed = Date.parse(ts);
  return Number.isFinite(parsed) ? parsed : null;
}

type KeyedEventRow = { event: SessionEvent; key: number };

// A thought's end is the start of the next item in the same turn. A user or
// interrupt row after a thought includes idle time, so those yield no
// duration — omitted silently per the fidelity contract.
export function thoughtDurations(rows: readonly KeyedEventRow[]): Map<number, number> {
  const durations = new Map<number, number>();
  for (let index = 0; index < rows.length; index += 1) {
    const row = rows[index];
    if (row.event.kind !== "thinking") continue;
    const next = rows[index + 1];
    if (!next || next.event.kind === "user" || next.event.kind === "interrupt") continue;
    const start = parseTs(row.event.ts);
    const end = parseTs(next.event.ts);
    if (start === null || end === null || end < start) continue;
    durations.set(row.key, end - start);
  }
  return durations;
}

function rowEndTs(row: KeyedEventRow): number | null {
  return parseTs(row.event.tool?.completed_at ?? row.event.ts);
}

// OpenCode ends every completed turn with `▣ Agent · model · duration`
// (session/index.tsx:1534-1559; duration = assistant completed − parent user
// created). Wiki equivalent: the LAST row of each turn opened by a real user
// message. The live turn is excluded — its final row carries the live-state
// indicator instead.
export function turnMetaDurations(
  rows: readonly KeyedEventRow[],
  working: boolean,
): Map<number, number | null> {
  const metas = new Map<number, number | null>();
  let turnStartTs: number | null = null;
  let inTurn = false;
  let lastRow: KeyedEventRow | null = null;
  const closeTurn = () => {
    if (!inTurn || !lastRow) return;
    const end = rowEndTs(lastRow);
    metas.set(lastRow.key, turnStartTs !== null && end !== null && end >= turnStartTs ? end - turnStartTs : null);
  };
  for (const row of rows) {
    if (row.event.kind === "user" && !row.event.source) {
      closeTurn();
      turnStartTs = parseTs(row.event.ts);
      inTurn = true;
      lastRow = null;
      continue;
    }
    if (inTurn) lastRow = row;
  }
  if (!working) closeTurn();
  return metas;
}

// OpenCode's per-tool icon micro-vocabulary (session/index.tsx:2090,2138,
// 2162,2186,2198,2206,2291,1808). State lives in the row COLOR; the glyph
// carries the verb family.
export function toolGlyph(tool: SessionTool, status: string): string {
  if (isBashTool(tool)) return "$";
  const name = toolName(tool);
  if (tool.archetype === "agent" || ["task", "agent"].includes(name)) {
    return status === "working" ? "│" : "✓";
  }
  if (tool.archetype === "read" || ["read", "notebookread"].includes(name)) return "→";
  if (tool.archetype === "edit" || ["edit", "multiedit", "notebookedit", "apply_patch", "write", "writefile"].includes(name)) return "←";
  if (tool.archetype === "search" || ["grep", "glob"].includes(name)) return "✱";
  if (["webfetch", "web_fetch", "fetch"].includes(name)) return "%";
  if (["websearch", "web_search"].includes(name)) return "◈";
  return "⚙";
}

const INTERPRETER_LANGS: Record<string, string> = {
  python: "python",
  python3: "python",
  node: "javascript",
  deno: "javascript",
  bun: "javascript",
  ruby: "ruby",
  bash: "bash",
  sh: "bash",
  zsh: "bash",
};

export type CommandSegment =
  | { kind: "bash"; text: string }
  | { kind: "embed-block"; lang: string; text: string }
  | { kind: "embed-inline"; lang: string; text: string };

const HEREDOC_OPENER = /<<(-?)\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))/g;
const INTERPRETER_TOKEN = /(?:^|[|&;(\s])([a-z0-9_]+)(?=\s)/g;
const ONE_LINER = /\b(python3?|node|deno|bun|ruby)\b(?:\s+[^\s'"|&;<>]+)*\s+-[ce]\s+(?:'([^']*)'|"((?:[^"\\]|\\.)*)")/;

function lineInterpreterLang(preamble: string): string | null {
  let lang: string | null = null;
  for (const match of preamble.matchAll(INTERPRETER_TOKEN)) {
    const mapped = INTERPRETER_LANGS[match[1]];
    if (mapped && mapped !== "bash") lang = mapped;
  }
  return lang;
}

// Render-time structure for bash commands that embed scripts: interpreter
// heredocs (`python3 - <<'PY' … PY`) and quoted `-c`/`-e` one-liner bodies.
// STRICT grammar — anything ambiguous (two heredoc openers on one line, an
// unterminated tag) returns null and the caller keeps the flat bash render;
// misattribution is worse than no structure. Unknown-interpreter heredoc
// bodies (cat configs etc.) are still consumed as opaque text so a script
// inside them can never be claimed. Segments concatenate back to the exact
// command; the raw toggle is untouched either way.
export function parseEmbeddedScripts(command: string): CommandSegment[] | null {
  if (!command.includes("\n") && !ONE_LINER.test(command)) return null;
  const lines = command.split("\n");
  const segments: CommandSegment[] = [];
  // Invariant: concatenating all segment texts reproduces `command` exactly.
  // Bash runs own every newline; embed bodies carry none at their edges.
  let bashRun: string[] = [];
  let sawEmbed = false;
  const flushBash = (trailingNewline: boolean) => {
    const text = bashRun.join("\n") + (trailingNewline ? "\n" : "");
    if (text) segments.push({ kind: "bash", text });
    bashRun = [];
  };
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    const openers = [...line.matchAll(HEREDOC_OPENER)];
    if (openers.length > 1) return null;
    if (openers.length === 1) {
      const [opener] = openers;
      const dashed = opener[1] === "-";
      const tag = opener[2] ?? opener[3] ?? opener[4];
      const closer = dashed ? new RegExp(`^\\t*${tag}$`) : new RegExp(`^${tag}$`);
      let end = -1;
      for (let scan = index + 1; scan < lines.length; scan += 1) {
        if (closer.test(lines[scan])) {
          end = scan;
          break;
        }
      }
      if (end === -1) return null;
      const body = lines.slice(index + 1, end).join("\n");
      const lang = lineInterpreterLang(line.slice(0, opener.index));
      if (lang && body) {
        bashRun.push(line);
        flushBash(true);
        segments.push({ kind: "embed-block", lang, text: body });
        // The newline before the closing tag belongs to the next bash run.
        bashRun.push("", lines[end]);
        sawEmbed = true;
      } else {
        bashRun.push(...lines.slice(index, end + 1));
      }
      index = end + 1;
      continue;
    }
    const oneLiner = line.match(ONE_LINER);
    const body = oneLiner ? oneLiner[2] ?? oneLiner[3] ?? "" : "";
    if (oneLiner && body) {
      const lang = INTERPRETER_LANGS[oneLiner[1]] ?? null;
      const start = oneLiner.index! + oneLiner[0].length - body.length - 1;
      if (lang) {
        bashRun.push(line.slice(0, start));
        flushBash(false);
        segments.push({ kind: "embed-inline", lang, text: body });
        bashRun.push(line.slice(start + body.length));
        sawEmbed = true;
        index += 1;
        continue;
      }
    }
    bashRun.push(line);
    index += 1;
  }
  flushBash(false);
  if (!sawEmbed) return null;
  return segments;
}

const FILE_READ_CMDS = new Set(["sed", "cat", "head", "tail", "awk"]);
const PATHISH = /[^./]\.[A-Za-z0-9]+$/;

// A bash command that is a recognizable file-content read (sed -n / cat /
// head / tail / awk over one file, possibly behind cd/&&) exposes its target
// path so the OUTPUT can highlight in the file's language. Conservative:
// pipes, redirections, grep-style decorated output, multiple candidate
// files, or anything ambiguous returns null and the output stays plain.
export function bashReadTargetPath(command: string): string | null {
  if (/[|<>]/.test(command) || command.includes("\n")) return null;
  const segments = command.split(/&&|;/).map((segment) => segment.trim()).filter(Boolean);
  while (segments.length > 1 && /^cd\s/.test(segments[0])) segments.shift();
  if (segments.length !== 1) return null;
  const tokens = segments[0].split(/\s+/);
  const cmd = tokens[0]?.split("/").pop() ?? "";
  if (!FILE_READ_CMDS.has(cmd)) return null;
  const candidates = tokens.slice(1)
    .map((token) => token.replace(/^['"]|['"]$/g, ""))
    .filter((token) => token && !token.startsWith("-") && !/[*?[\]{}]/.test(token) && PATHISH.test(token));
  return candidates.length === 1 ? candidates[0] : null;
}

const MAX_JSON_PRETTY_BYTES = 1_048_576;

// Parse-don't-guess: a payload is JSON exactly when JSON.parse accepts it.
// Only object/array payloads benefit from indentation.
export function prettyPrintedJson(text: string): string | null {
  const trimmed = text.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_JSON_PRETTY_BYTES) return null;
  const first = trimmed[0];
  if (first !== "{" && first !== "[") return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null || typeof parsed !== "object") return null;
    const pretty = JSON.stringify(parsed, null, 2);
    return pretty === trimmed ? null : pretty;
  } catch {
    return null;
  }
}

const SHEBANG_LANGS: Array<[RegExp, string]> = [
  [/^#!\s*\S*\b(?:bash|sh|zsh)\b/, "bash"],
  [/^#!\s*\S*\bpython[0-9.]*\b/, "python"],
  [/^#!\s*\S*\bnode\b/, "javascript"],
  [/^#!\s*\S*\bruby\b/, "ruby"],
];

export type StructuredContent = {
  lang: string;
  text: string;
  code: boolean;
};

// Conservative content-kind detection for the polished/pretty surfaces:
// a language hint (file extension, MCP schema field) wins; otherwise JSON
// must parse and shebangs must match. Unknown content stays plain — no
// heuristic guessing.
export function detectStructuredContent(
  text: string,
  langHint: string | null = null,
): StructuredContent | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  if (langHint === "json" || (!langHint && (trimmed.startsWith("{") || trimmed.startsWith("[")))) {
    const pretty = prettyPrintedJson(trimmed);
    if (pretty !== null) return { lang: "json", text: pretty, code: false };
    if (langHint === "json") return { lang: "json", text, code: true };
  }
  if (langHint) return { lang: langHint, text, code: true };
  for (const [pattern, lang] of SHEBANG_LANGS) {
    if (pattern.test(trimmed)) return { lang, text, code: true };
  }
  return null;
}

const PYTHON_SIGNATURE = /^(?:def\s+\w+\s*\(|class\s+\w+[:(]|from\s+\S+\s+import\b|import\s+\w+(?:\s*,\s*\w+)*\s*$|if\s+__name__\s*==)/;

// Untagged fences in agent prose: conservative signature detection only —
// shebangs, JSON that parses, unmistakable python, `$ `-prefixed shell.
// Anything else stays a plain block; the agent's own language tag always
// wins upstream.
export function detectFenceLang(code: string): string | null {
  const trimmed = code.trim();
  if (!trimmed) return null;
  for (const [pattern, lang] of SHEBANG_LANGS) {
    if (pattern.test(trimmed)) return lang;
  }
  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed !== null && typeof parsed === "object") return "json";
    } catch {
      return null;
    }
    return null;
  }
  if (trimmed.startsWith("$ ")) return "bash";
  const lines = trimmed.split("\n");
  let pythonHits = 0;
  for (const line of lines) {
    if (PYTHON_SIGNATURE.test(line.trim())) pythonHits += 1;
    if (pythonHits >= 2) return "python";
  }
  if (pythonHits === 1 && lines.length === 1) return "python";
  return null;
}

// WIKI-253: default-collapse threshold for tool-output blocks. Anything
// taller than this many pixels (measured after render) collapses behind a
// one-line peek row until the reader clicks to expand. Height replaces the
// prior line-count/char-count heuristic — a 12-line block of 200-char lines
// is much taller than 12 lines of 20 chars, and a pixel budget captures the
// intent ("keep the turn scannable") in one tuneable dial.
export const COLLAPSE_HEIGHT_PX = 240;
// Cheap pre-filter: "is this output long enough that we should promote it
// from the inline pill into a block-shaped renderer so the height gate can
// see it?" Line count is fine here because the pre-filter only decides which
// component tree to render — the collapse decision itself is pixel-measured.
const TOOL_OUTPUT_PROMOTION_LINES = 12;
// Retained for existing tests that generate synthetic long outputs against
// the promotion threshold — kept in sync with the render-time pre-filter.
export const TOOL_OUTPUT_COLLAPSE_LINES = TOOL_OUTPUT_PROMOTION_LINES;

// Would an inline tool render tall enough that it should be promoted to a
// block-shaped renderer for the collapse gate to apply? Called from the
// render layer alongside toolPresentation; a `true` result routes read_agent
// / list_agents / arbitrary MCP output through the same peek-and-expand
// affordance as bash and agent tool outputs.
export function wouldRenderTall(displayOutput: string, _tool: SessionTool): boolean {
  if (!displayOutput) return false;
  const lineCount = displayOutput.split("\n").length;
  return lineCount > TOOL_OUTPUT_PROMOTION_LINES;
}

function formatBytesShort(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function countBytes(text: string): number {
  if (typeof TextEncoder !== "undefined") return new TextEncoder().encode(text).length;
  let bytes = 0;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    if (code < 0x80) bytes += 1;
    else if (code < 0x800) bytes += 2;
    else if (code >= 0xd800 && code <= 0xdbff) { bytes += 4; i += 1; }
    else bytes += 3;
  }
  return bytes;
}

function firstNonEmptyLine(text: string, budget = 72): string {
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    return trimmed.length > budget ? `${trimmed.slice(0, budget - 1)}…` : trimmed;
  }
  return "";
}

function jsonPeek(text: string): string | null {
  const trimmed = text.trim();
  if (!trimmed) return null;
  const first = trimmed[0];
  if (first !== "{" && first !== "[") return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (Array.isArray(parsed)) {
      return `[${parsed.length} item${parsed.length === 1 ? "" : "s"}]`;
    }
    if (parsed !== null && typeof parsed === "object") {
      const keys = Object.keys(parsed as Record<string, unknown>);
      if (keys.length === 0) return "{}";
      const shown = keys.slice(0, 4).join(", ");
      const suffix = keys.length > 4 ? `, +${keys.length - 4}` : "";
      return `{${shown}${suffix}}`;
    }
    return null;
  } catch {
    return null;
  }
}

export type ToolOutputPeek = {
  // A short structural label — read as `<preview> · <count> lines · <size>`.
  preview: string;
  size: string;
  lines: number;
};

// Preserve the basename when a path is longer than the peek row can fit.
// End-truncation would eat the identifying tail (README.md), which is the
// most useful piece; middle-truncation keeps head and basename with `…` in
// between. Budget is generous — narrower containers still get a CSS ellipsis
// fallback, but the common wide row shows the whole `first N…last M` form.
export function middleTruncatePath(path: string, budget = 60): string {
  if (path.length <= budget) return path;
  // Anchor the tail on the basename plus any parent directory that fits, so
  // "/a/b/very/nested/README.md" reads as "/a/b/very…nested/README.md" rather
  // than losing the parent context along with the head.
  const slash = path.lastIndexOf("/");
  const basename = slash >= 0 ? path.slice(slash) : path;
  const tail = basename.length + 1 >= budget
    ? basename.slice(-(budget - 2))
    : basename;
  const headBudget = Math.max(1, budget - tail.length - 1);
  return `${path.slice(0, headBudget)}…${tail}`;
}

// Peek row content for a collapsed tool-output block (WIKI-253). Bash reads
// surface the target path; JSON payloads surface top-level keys / item counts;
// everything else falls back to the first non-empty line. The count/size tail
// is always present so the reader can tell how much they're hiding.
export function toolOutputPeek(tool: SessionTool, text: string): ToolOutputPeek {
  const cleaned = text.replace(/\s+$/, "");
  const lines = cleaned.length === 0 ? 0 : cleaned.split("\n").length;
  const size = formatBytesShort(countBytes(cleaned));
  const bashTarget = isBashTool(tool) ? bashReadTargetPath(tool.input) : null;
  if (bashTarget) return { preview: middleTruncatePath(bashTarget), size, lines };
  const json = jsonPeek(cleaned);
  if (json) return { preview: json, size, lines };
  const preview = firstNonEmptyLine(cleaned);
  return { preview: preview || "(empty)", size, lines };
}

// Conservative path extraction for filetype hints: structured fields only
// (edit payload, JSON input fields), then the summary target when it reads
// as a real path token.
export function toolPathHint(tool: SessionTool): string | null {
  if (tool.edit?.file_path) return tool.edit.file_path;
  const trimmed = tool.input.trim();
  if (trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        const record = parsed as Record<string, unknown>;
        for (const key of ["file_path", "filePath", "path", "notebook_path"]) {
          const value = record[key];
          if (typeof value === "string" && value.trim()) return value.trim();
        }
      }
    } catch {
      // fall through to the summary target
    }
  }
  const { target } = toolSummaryParts(tool);
  const token = target.split(/\s+/)[0] ?? "";
  if (token && !token.includes("{") && /[^./]\.[A-Za-z0-9]+$/.test(token)) return token;
  return null;
}

// Inline detail stays tier-1 dense: JSON object inputs collapse to OpenCode's
// `[key=value, …]` form (session/index.tsx:2613-2620) instead of raw JSON.
export function compactJsonDetail(input: string): string | null {
  const trimmed = input.trim();
  if (!trimmed.startsWith("{")) return null;
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const primitives = Object.entries(parsed as Record<string, unknown>).filter(([, value]) =>
      typeof value === "string" || typeof value === "number" || typeof value === "boolean",
    );
    if (primitives.length === 0) return null;
    return primitives
      .map(([key, value]) => `${key}: ${typeof value === "string" ? value : String(value)}`)
      .join(" · ");
  } catch {
    return null;
  }
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
