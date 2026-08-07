// WIKI-249 OpenCode 1:1 fidelity pins: turn meta row anatomy, thought
// duration format, click-to-expand placement, glyph vocabulary, color-only
// tool state, structured-output pretty rendering, the polished disclosure,
// and agent-declared fence handling.
import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import {
  bashReadTargetPath,
  compactJsonDetail,
  detectFenceLang,
  detectStructuredContent,
  formatEventDuration,
  parseEmbeddedScripts,
  prettyPrintedJson,
  thoughtDurations,
  toolGlyph,
  toolPathHint,
  turnMetaDurations,
} from "../src/transcript-event-utils";
import { MarkdownPre } from "../src/markdown";
import { ActivityEventRow, ToolCallRow, TurnMetaRow } from "../src/session";

class NoopObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  (globalThis as { ResizeObserver?: unknown }).ResizeObserver ??= NoopObserver;
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function tool(overrides: Partial<SessionTool> = {}): SessionTool {
  return {
    name: "Read",
    input: "frontend/src/session.tsx",
    output: "content",
    ok: true,
    archetype: "read",
    summary: "read session.tsx",
    ...overrides,
  };
}

function event(id: number, overrides: Partial<SessionEvent> = {}): SessionEvent {
  return {
    id,
    kind: "tool",
    ts: null,
    text: "",
    disposition: "rendered",
    tool: tool(),
    ...overrides,
  };
}

// --- duration vocabulary (OpenCode locale.ts:39) ---

test("formatEventDuration matches the OpenCode duration vocabulary", () => {
  expect(formatEventDuration(202)).toBe("202ms");
  expect(formatEventDuration(1100)).toBe("1.1s");
  expect(formatEventDuration(2500)).toBe("2.5s");
  expect(formatEventDuration(80_000)).toBe("1m 20s");
  expect(formatEventDuration(3_720_000)).toBe("1h 2m");
  expect(formatEventDuration(90_000_000)).toBe("1d 1h");
});

// --- thought durations: next event in the same turn bounds the thought ---

test("thoughtDurations uses the next same-turn event and omits user boundaries", () => {
  const rows = [
    { key: 1, event: event(1, { kind: "thinking", text: "a", ts: "2026-08-06T10:00:00.000Z" }) },
    { key: 2, event: event(2, { ts: "2026-08-06T10:00:00.463Z" }) },
    { key: 3, event: event(3, { kind: "thinking", text: "b", ts: "2026-08-06T10:00:05.000Z" }) },
    { key: 4, event: event(4, { kind: "user", text: "next", tool: undefined }) },
  ];
  const durations = thoughtDurations(rows);
  expect(durations.get(1)).toBe(463);
  expect(durations.has(3)).toBe(false);
});

// --- turn meta rows: last row of each completed turn, duration user→end ---

test("turnMetaDurations marks the final row of completed turns only", () => {
  const rows = [
    { key: 0, event: event(0, { kind: "user", text: "go", tool: undefined, ts: "2026-08-06T10:00:00.000Z" }) },
    { key: 1, event: event(1, { ts: "2026-08-06T10:00:01.000Z" }) },
    { key: 2, event: event(2, { kind: "assistant", text: "done", tool: undefined, ts: "2026-08-06T10:00:02.500Z" }) },
    { key: 3, event: event(3, { kind: "user", text: "again", tool: undefined, ts: "2026-08-06T10:01:00.000Z" }) },
    { key: 4, event: event(4, { ts: "2026-08-06T10:01:04.000Z" }) },
  ];
  const idle = turnMetaDurations(rows, false);
  expect(idle.get(2)).toBe(2500);
  expect(idle.get(4)).toBe(4000);
  expect(idle.size).toBe(2);
  // The live turn keeps the live-state indicator instead of a meta row.
  const working = turnMetaDurations(rows, true);
  expect(working.get(2)).toBe(2500);
  expect(working.has(4)).toBe(false);
});

test("turn meta row renders the OpenCode boundary anatomy", () => {
  const { container } = render(
    <TurnMetaRow meta={{ agent: "cc:WIKI-249", model: "claude-fable-5", durationMs: 2500 }} />,
  );
  const row = container.querySelector(".session-turn-meta");
  expect(row?.textContent).toBe("▣cc:WIKI-249 · claude-fable-5 · 2.5s");
  expect(row?.querySelector(".session-turn-meta-glyph")?.getAttribute("aria-hidden")).toBe("true");
});

test("turn meta row omits missing model and duration silently", () => {
  const { container } = render(
    <TurnMetaRow meta={{ agent: "cc:WIKI-249", model: null, durationMs: null }} />,
  );
  expect(container.querySelector(".session-turn-meta")?.textContent).toBe("▣cc:WIKI-249");
});

// --- thought row anatomy (OpenCode index.tsx:1655-1673) ---

test("thought row reads `+ Thought: title · duration` and flips to `-` open", () => {
  const thinking = event(9, { kind: "thinking", text: "compare the renderers\nbody detail", tool: undefined });
  const { container, getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={9} thoughtDurationMs={463} ticket="WIKI-249" />,
  );
  const head = getByRole("button");
  expect(head.textContent).toBe("+Thought: compare the renderers · 463ms");
  expect(head.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector(".session-activity-row-meta")).toBeNull();
  fireEvent.click(head);
  expect(head.textContent?.startsWith("-Thought:")).toBe(true);
  expect(container.querySelector(".session-thinking")?.textContent).toContain("body detail");
});

test("thought row without timing omits the duration suffix", () => {
  const thinking = event(9, { kind: "thinking", text: "quiet", tool: undefined });
  const { getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={9} ticket="WIKI-249" />,
  );
  expect(getByRole("button").textContent).toBe("+Thought: quiet");
});

test("Codex detailed summaries render collapsed and normalize bold title wrappers", () => {
  const thinking = event(10, {
    kind: "thinking",
    text: "**Planning gate loop verification and CI checks**\nInspect every supervisor-owned role.\nKeep the shared renderer unchanged.",
    encrypted: true,
    tool: undefined,
  });
  const { container, getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={10} ticket="WIKI-265" />,
  );
  const head = getByRole("button");
  expect(head.getAttribute("aria-expanded")).toBe("false");
  expect(head.textContent).toContain("Thought: Planning gate loop verification and CI checks");
  expect(head.textContent).not.toContain("**");
  expect(container.querySelector(".session-thinking")).toBeNull();
  fireEvent.click(head);
  expect(container.querySelector(".session-thinking")?.textContent).toContain("Inspect every supervisor-owned role.");
  expect(container.querySelector(".session-thinking")?.textContent).not.toContain("**Planning");
});

test("truthy Codex capability markers keep summaries collapsed", () => {
  const thinking = event(17, {
    kind: "thinking",
    text: "**Exploring agent/events terminal module**",
    encrypted: "codex-summary",
    tool: undefined,
  });
  const { container, getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={17} ticket="WIKI-267" />,
  );
  const head = getByRole("button");
  expect(head.getAttribute("aria-expanded")).toBe("false");
  expect(head.textContent).not.toContain("**");
  expect(container.querySelector(".session-thinking")).toBeNull();
  fireEvent.click(head);
  expect(container.querySelector(".session-thinking")?.textContent).toBe(
    "Exploring agent/events terminal module",
  );
});

test("Codex adjacent same-line and newline fragments render as separate thought rows", () => {
  const fragments = [
    event(12, { kind: "thinking", text: "**thought one**", encrypted: true, tool: undefined }),
    event(13, { kind: "thinking", text: "**thought two**", encrypted: true, tool: undefined }),
    event(14, { kind: "thinking", text: "**thought three**", encrypted: true, tool: undefined }),
    event(15, { kind: "thinking", text: "**thought four**", encrypted: true, tool: undefined }),
  ];
  const { container } = render(
    <>
      {fragments.map((fragment) => (
        <ActivityEventRow event={fragment} rowKey={fragment.id} ticket="WIKI-265" key={fragment.id} />
      ))}
    </>,
  );
  const heads = Array.from(container.querySelectorAll(".session-thinking-head"));
  expect(heads).toHaveLength(4);
  expect(heads.map((head) => head.textContent)).toEqual([
    "+Thought: thought oneencrypted",
    "+Thought: thought twoencrypted",
    "+Thought: thought threeencrypted",
    "+Thought: thought fourencrypted",
  ]);
});

test("Codex ambiguous bold prose stays one honest unsplit thought", () => {
  const thinking = event(16, {
    kind: "thinking",
    text: "**thought one** and **thought two**",
    encrypted: true,
    tool: undefined,
  });
  const { getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={16} ticket="WIKI-265" />,
  );
  expect(getByRole("button").textContent).toContain("**thought one** and **thought two**");
});

test("non-Codex thinking keeps the existing collapsed behavior", () => {
  const thinking = event(11, {
    kind: "thinking",
    text: "**Claude summary**\nClaude body detail",
    tool: undefined,
  });
  const { container, getByRole } = render(
    <ActivityEventRow event={thinking} rowKey={11} ticket="WIKI-265" />,
  );
  const head = getByRole("button");
  expect(head.getAttribute("aria-expanded")).toBe("false");
  expect(head.textContent).toContain("**Claude summary**");
  expect(container.querySelector(".session-thinking")).toBeNull();
});

// --- glyph vocabulary + color-only state ---

test("toolGlyph maps the OpenCode icon vocabulary", () => {
  expect(toolGlyph(tool({ archetype: "bash", name: "Bash" }), "done")).toBe("$");
  expect(toolGlyph(tool(), "done")).toBe("→");
  expect(toolGlyph(tool({ archetype: "edit", name: "Edit" }), "done")).toBe("←");
  expect(toolGlyph(tool({ archetype: "search", name: "Grep" }), "done")).toBe("✱");
  expect(toolGlyph(tool({ archetype: "agent", name: "Task" }), "working")).toBe("│");
  expect(toolGlyph(tool({ archetype: "agent", name: "Task" }), "done")).toBe("✓");
  expect(toolGlyph(tool({ archetype: "tool", name: "launch_thing" }), "done")).toBe("⚙");
});

test("tool rows carry state as color classes plus sr-only text, not words", () => {
  const { container } = render(
    <ToolCallRow event={event(1)} ticket="WIKI-249" withResult />,
  );
  expect(container.querySelector(".session-tool-status")).toBeNull();
  expect(container.querySelector(".session-tool")?.classList.contains("is-done")).toBe(true);
  expect(container.querySelector(".session-tool-icon-text")?.textContent).toBe("→");
  expect(container.querySelector(".sr-only")?.textContent).toBe("done");
});

test("equivalent Claude and Codex canonical tools use the same shared row", () => {
  const claudeRaw = {
    name: "Read",
    input: { file_path: "src/main.py" },
    result: "line one",
  };
  const claudeCanonical = tool({
    name: claudeRaw.name,
    input: claudeRaw.input.file_path,
    output: claudeRaw.result,
    archetype: "read",
    summary: "read main.py",
  });
  const codexRaw = {
    name: "Read",
    arguments: '{"file_path":"src/main.py"}',
    output: "line one",
  };
  const codexArguments = JSON.parse(codexRaw.arguments) as { file_path: string };
  const codexCanonical = tool({
    name: codexRaw.name,
    input: codexArguments.file_path,
    output: codexRaw.output,
    archetype: "read",
    summary: "read main.py",
  });
  for (const field of ["name", "input", "output", "archetype", "summary"] as const) {
    expect(codexCanonical[field]).toBe(claudeCanonical[field]);
  }
  const { container } = render(
    <>
      <ToolCallRow event={event(20, { tool: claudeCanonical })} ticket="WIKI-265" withResult />
      <ToolCallRow event={event(21, { tool: codexCanonical })} ticket="WIKI-265" withResult />
    </>,
  );
  const rows = Array.from(container.querySelectorAll(".session-tool"));
  expect(rows).toHaveLength(2);
  expect(rows[0].className).toBe(rows[1].className);
  expect(rows[0].querySelector(".session-tool-summary")?.textContent)
    .toBe(rows[1].querySelector(".session-tool-summary")?.textContent);
});

// --- click-to-expand placement (OpenCode index.tsx:2083-2085) ---

test("long bash blocks default-collapse to a peek row that expands on click", () => {
  // WIKI-253: past the 12-line collapse threshold, the block hides behind a
  // one-line peek row (chevron + preview + size); one click reveals the body
  // and a matching collapse affordance appears.
  const lines = Array.from({ length: 14 }, (_, i) => `line ${i + 1}`).join("\n");
  const bashTool = tool({ archetype: "bash", name: "Bash", input: "seq 14", summary: "bash seq 14", output: lines });
  const { container } = render(
    <ToolCallRow event={event(2, { tool: bashTool })} ticket="WIKI-249" withResult />,
  );
  const peek = container.querySelector<HTMLButtonElement>(".session-tool-output-peek");
  expect(peek).toBeTruthy();
  expect(peek?.getAttribute("aria-expanded")).toBe("false");
  expect(container.querySelector(".transcript-preview")).toBeNull();
  fireEvent.click(peek!);
  expect(container.querySelector(".session-tool-output-peek")).toBeNull();
  expect(container.querySelector(".session-tool-output-collapse")).toBeTruthy();
  expect(container.querySelector(".transcript-preview")).toBeTruthy();
});

// --- structured output: pretty in block views, compact inline ---

test("one-line JSON block output renders indented; raw keeps exact bytes", () => {
  const json = "{\"lang\": \"shiki\", \"ok\": true, \"count\": 42}";
  const monitorTool = tool({ archetype: "tool", name: "monitor", input: "", summary: "monitor run", output: json });
  const { container } = render(
    <ToolCallRow event={event(3, { tool: monitorTool })} ticket="WIKI-249" withResult />,
  );
  const bodyText = container.querySelector(".session-tool-output-text")?.textContent ?? "";
  expect(bodyText).toContain("{\n  \"lang\": \"shiki\"");
  expect(container.querySelector(".syntax-inline")).toBeTruthy();
});

test("inline JSON results stay single-line under the tier-1 clip", () => {
  const json = "{\"status\": \"ok\", \"latency\": 12}";
  const mcpTool = tool({ archetype: "tool", name: "launch_thing", input: "", summary: "launch_thing", output: json });
  const { container } = render(
    <ToolCallRow event={event(4, { tool: mcpTool })} ticket="WIKI-249" withResult />,
  );
  const inline = container.querySelector(".session-tool-inline-result");
  expect(inline).toBeTruthy();
  expect(inline?.textContent).not.toContain("\n");
});

test("malformed JSON is left untouched", () => {
  expect(prettyPrintedJson("{\"broken\": ")).toBeNull();
  expect(detectStructuredContent("{\"broken\": ")).toBeNull();
  expect(prettyPrintedJson("plain sentence")).toBeNull();
});

test("compactJsonDetail collapses JSON inputs to key: value pairs", () => {
  expect(compactJsonDetail("{\"question\": \"Continue?\", \"count\": 2}"))
    .toBe("question: Continue? · count: 2");
  expect(compactJsonDetail("not json")).toBeNull();
});

// --- polished disclosure: sibling of raw, bounded, formatted ---

test("polish toggle reveals a formatted view over the default raw peek", () => {
  const py = "def main():\n    print(\"hi\")\n\nif __name__ == \"__main__\":\n    main()";
  const readTool = tool({
    name: "Read",
    archetype: "read",
    input: "{\"file_path\": \"scripts/run.py\"}",
    summary: "read scripts/run.py",
    output: py,
  });
  const { container, getByRole, queryByRole } = render(
    <ToolCallRow event={event(5, { tool: readTool })} ticket="WIKI-249" withResult />,
  );
  const toggle = getByRole("button", { name: "show polished output" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
  expect(container.querySelector(".session-tool-polished")).toBeNull();
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(toggle.getAttribute("aria-pressed")).toBe("true");
  const polishedView = container.querySelector(".session-tool-polished");
  expect(polishedView?.textContent).toContain("def main():");
  // Code-like content carries line-number gutters.
  expect(polishedView?.querySelector(".syntax-inline-gutter")?.textContent).toBe("1");
  // Toggling again returns to the default raw peek.
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-pressed")).toBe("false");
  expect(container.querySelector(".session-tool-polished")).toBeNull();
  // The redundant separate "raw" toggle no longer exists — raw IS the default.
  expect(queryByRole("button", { name: "show raw output" })).toBeNull();
  expect(container.querySelector(".session-tool-raw-toggle")).toBeNull();
});

test("polished is not offered for unrecognizable output", () => {
  const opaque = tool({ archetype: "tool", name: "launch_thing", input: "", summary: "launch_thing", output: "three plain words" });
  const { queryByRole } = render(
    <ToolCallRow event={event(6, { tool: opaque })} ticket="WIKI-249" withResult />,
  );
  expect(queryByRole("button", { name: "show polished output" })).toBeNull();
});

// --- toolPathHint conservatism ---

test("toolPathHint reads structured fields first, then path-like targets", () => {
  expect(toolPathHint(tool({ input: "{\"file_path\": \"a/b.py\"}" }))).toBe("a/b.py");
  expect(toolPathHint(tool({ edit: { file_path: "c.ts" }, input: "" }))).toBe("c.ts");
  expect(toolPathHint(tool({ input: "", summary: "read frontend/src/x.tsx" }))).toBe("frontend/src/x.tsx");
  expect(toolPathHint(tool({ input: "", summary: "ask a question" }))).toBeNull();
});

// --- embedded scripts in bash commands ---

test("python heredoc splits into bash preamble, python body, bash postamble", () => {
  const command = "python3 - <<'PY'\np = 'vault/todo.md'\nprint('ok')\nPY\ngit add vault/todo.md && git push -q origin main";
  const segments = parseEmbeddedScripts(command);
  expect(segments).not.toBeNull();
  expect(segments!.map((segment) => segment.kind)).toEqual(["bash", "embed-block", "bash"]);
  const [pre, body, post] = segments!;
  expect(body).toMatchObject({ lang: "python", text: "p = 'vault/todo.md'\nprint('ok')" });
  expect(pre.text).toBe("python3 - <<'PY'\n");
  expect(post.text).toBe("\nPY\ngit add vault/todo.md && git push -q origin main");
  // Exact-bytes invariant: segments concatenate back to the command.
  expect(segments!.map((segment) => segment.text).join("")).toBe(command);
});

test("-c one-liner bodies highlight as the interpreter language inline", () => {
  const command = "python3 -c 'import json; print(json.dumps({}))' && echo done";
  const segments = parseEmbeddedScripts(command);
  expect(segments).not.toBeNull();
  const embed = segments!.find((segment) => segment.kind === "embed-inline");
  expect(embed).toMatchObject({ lang: "python", text: "import json; print(json.dumps({}))" });
  expect(segments!.map((segment) => segment.text).join("")).toBe(command);
});

test("mismatched heredoc tags fall back to flat bash", () => {
  expect(parseEmbeddedScripts("python3 - <<'PY'\nprint('never closed')\nEOF")).toBeNull();
  // Two openers on one line is ambiguous — never misattribute.
  expect(parseEmbeddedScripts("python3 - <<'PY' <<'ALSO'\nx\nPY")).toBeNull();
});

test("unknown-interpreter heredoc bodies are consumed, not claimed", () => {
  const command = "cat > f <<'EOF'\npython3 - <<'PY'\nEOF\necho done";
  expect(parseEmbeddedScripts(command)).toBeNull();
});

test("bash titles render embedded heredoc bodies as separated sections", () => {
  const command = "python3 - <<'PY'\nprint('ok')\nPY\ngit add -A";
  const bashTool = tool({ archetype: "bash", name: "Bash", input: command, summary: "bash python3 -", output: "ok" });
  const { container } = render(
    <ToolCallRow event={event(7, { tool: bashTool })} ticket="WIKI-249" withResult />,
  );
  const title = container.querySelector(".session-tool-block-title.is-command");
  const embed = title?.querySelector(".session-command-embed");
  expect(embed?.getAttribute("data-lang")).toBe("python");
  expect(embed?.textContent).toBe("print('ok')");
  expect(title?.textContent).toContain("python3 - <<'PY'");
  expect(title?.textContent).toContain("git add -A");
});

// --- file-slice output language inference ---

test("bashReadTargetPath finds the single file behind cd/&& read commands", () => {
  expect(bashReadTargetPath("cd /app && sed -n '85,130p' backend/settings.py")).toBe("backend/settings.py");
  expect(bashReadTargetPath("cat frontend/src/x.ts")).toBe("frontend/src/x.ts");
  expect(bashReadTargetPath("head -n 50 notes.txt")).toBe("notes.txt");
  // Pipelines, multiple files, non-read commands: never inferred.
  expect(bashReadTargetPath("grep -n foo x.py | head -3")).toBeNull();
  expect(bashReadTargetPath("cat a.py b.py")).toBeNull();
  expect(bashReadTargetPath("sed -n 1p x.py > out.py")).toBeNull();
  expect(bashReadTargetPath("git show HEAD:x.py")).toBeNull();
});

test("sed-slice .py output highlights as python; .txt stays plain", () => {
  const py = "def handler():\n    return 1";
  const slice = tool({
    archetype: "bash",
    name: "Bash",
    input: "cd /app && sed -n '85,130p' backend/settings.py",
    summary: "bash sed -n",
    output: py,
  });
  const { container } = render(
    <ToolCallRow event={event(8, { tool: slice })} ticket="WIKI-249" withResult />,
  );
  const body = container.querySelector(".session-tool-output-text .syntax-inline");
  expect(body?.getAttribute("data-lang")).toBe("python");
  expect(body?.textContent).toContain("def handler():");
  cleanup();
  const txt = tool({ archetype: "bash", name: "Bash", input: "cat notes.txt", summary: "bash cat", output: "plain words" });
  const plain = render(<ToolCallRow event={event(9, { tool: txt })} ticket="WIKI-249" withResult />);
  expect(plain.container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});

// --- agent-declared fences ---

test("detectFenceLang: conservative signatures only", () => {
  expect(detectFenceLang("#!/bin/bash\nls")).toBe("bash");
  expect(detectFenceLang("{\"a\": 1}")).toBe("json");
  expect(detectFenceLang("def x():\n    pass\nclass Y:\n    pass")).toBe("python");
  expect(detectFenceLang("$ git status")).toBe("bash");
  expect(detectFenceLang("token = HMAC-SHA256(secret)[:N] as hex")).toBeNull();
  expect(detectFenceLang("just a sentence about code")).toBeNull();
});

function fence(code: string, lang?: string) {
  return (
    <MarkdownPre detectLang>
      <code className={lang ? `language-${lang}` : undefined}>{code}</code>
    </MarkdownPre>
  );
}

test("tagged fences keep their language; untagged detect or stay plain", () => {
  const tagged = render(fence("print('hi')", "python"));
  expect(tagged.container.querySelector(".markdown-code-block-wrap")?.getAttribute("data-lang")).toBe("python");
  cleanup();
  const detected = render(fence("def main():\n    pass\nif __name__ == \"__main__\":\n    main()"));
  expect(detected.container.querySelector(".markdown-code-block-wrap")?.getAttribute("data-lang")).toBe("python");
  cleanup();
  const plain = render(fence("token = HMAC-SHA256(secret)[:N] as hex"));
  expect(plain.container.querySelector(".markdown-code-block-wrap")?.getAttribute("data-lang")).toBeNull();
});
