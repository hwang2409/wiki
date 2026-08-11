// @vitest-environment jsdom
// WIKI-274: syntax-highlight file-read tool outputs across surfaces. Codex
// read rows never hit the bash file-slice branch (their archetype is `read`),
// so the language must fall out of the summary target (`read foo.yaml:1-10`)
// or, when the summary carries no filename, from the output content shape.
// Existing renderers keep priority: JSON pretty-print (WIKI-270), cat -n
// numbered reads (WIKI-261), ANSI-decorated bash output, and GitHub-anchor
// preview text all render unchanged.
import { cleanup, render } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import type { SessionEvent, SessionTool } from "../src/api";
import { SessionUiStateContext, ToolCallRow, type SessionUiState } from "../src/session";
import { toolPathHint } from "../src/transcript-event-utils";

afterEach(() => {
  cleanup();
});

// The WIKI-253 collapse gate (measured pixel height) hides tall tool outputs
// behind a peek row by default. Every test in this file drives the highlight
// branch, not the collapse behavior, so we pre-seed an "expanded" override in
// the per-session UI store so the block renders its body regardless of the
// jsdom offsetHeight stub.
function expandedUi(eventId: number): SessionUiState {
  const overrides = new Map<string, boolean>();
  overrides.set(`tool-output:${eventId}`, true);
  return { booleans: new Map(), overrides };
}

function withUi(ui: SessionUiState, node: React.ReactNode) {
  return <SessionUiStateContext.Provider value={ui}>{node}</SessionUiStateContext.Provider>;
}

function codexReadTool(overrides: Partial<SessionTool> = {}): SessionTool {
  // Codex file reads run through exec_command with a sed slice; classify_tool
  // turns them into archetype=read + summary="read <name>:<start>-<end>".
  return {
    name: "exec_command",
    input: "sed -n '569,596p' staging-deploy.yaml",
    output: "",
    ok: true,
    archetype: "read",
    summary: "read staging-deploy.yaml:569-596",
    ...overrides,
  };
}

function event(id: number, tool: SessionTool): SessionEvent {
  return { id, kind: "tool", ts: null, text: "", disposition: "rendered", tool };
}

// Payloads must exceed the WIKI-253 line-count promotion threshold so the
// inline read tool is promoted to a block and hits ToolOutputBody.
function longYamlPayload(): string {
  const lines: string[] = ["deploy:", "  stage: staging", "  region: us-east-1", ""];
  for (let i = 0; i < 14; i += 1) {
    lines.push(`  service_${i}:`);
    lines.push(`    replicas: ${i + 1}`);
  }
  return lines.join("\n");
}

test("toolPathHint strips a :N-M line-range suffix so read summaries resolve", () => {
  expect(toolPathHint(codexReadTool())).toBe("staging-deploy.yaml");
  expect(
    toolPathHint(
      codexReadTool({
        input: "sed -n '12p' app/store.py",
        summary: "read store.py:12",
      }),
    ),
  ).toBe("store.py");
  // No slice suffix — path passes through unchanged.
  expect(
    toolPathHint(
      codexReadTool({ input: "cat notes.md", summary: "read notes.md" }),
    ),
  ).toBe("notes.md");
});

test("codex read output highlights in the summary's file language", () => {
  const tool = codexReadTool({ output: longYamlPayload() });
  const { container } = render(withUi(expandedUi(1), <ToolCallRow event={event(1, tool)} ticket="WIKI-274" withResult />));
  const highlighted = container.querySelector(".session-tool-output-text .syntax-inline");
  expect(highlighted).toBeTruthy();
  expect(highlighted?.getAttribute("data-lang")).toBe("yaml");
});

test("pathless code-shaped output falls back to content-based language", () => {
  // Some read surfaces do not expose a filename in the summary; the shape of
  // the content still identifies python distinctly enough to highlight.
  const pythonSource = [
    "from dataclasses import dataclass",
    "",
    "def build(payload):",
    "    return payload",
    "",
    "class Runner:",
    "    def __init__(self, name: str) -> None:",
    "        self.name = name",
    "",
    "def run() -> None:",
    "    Runner('demo').name",
    "",
    "def more() -> None:",
    "    pass",
  ].join("\n");
  const tool: SessionTool = {
    name: "exec_command",
    input: "cat script",
    output: pythonSource,
    ok: true,
    archetype: "read",
    // No filename in the summary — content shape is the only hint.
    summary: "read script",
  };
  const { container } = render(withUi(expandedUi(2), <ToolCallRow event={event(2, tool)} ticket="WIKI-274" withResult />));
  const highlighted = container.querySelector(".session-tool-output-text .syntax-inline");
  expect(highlighted).toBeTruthy();
  expect(highlighted?.getAttribute("data-lang")).toBe("python");
});

test("JSON output keeps the pretty-print branch (WIKI-270 regression guard)", () => {
  // detectStructuredContent must still preempt readOutputLang so JSON payloads
  // land pretty-printed instead of highlighted raw. Uses a bash tool because
  // bash presentation is always block; the render precedence being asserted
  // (structured wins over readOutputLang) is the same for a read tool that
  // clears the WIKI-253 promotion gate.
  const record: Record<string, unknown> = { data: { repository: { name: "wiki" } } };
  for (let i = 0; i < 20; i += 1) record[`extra_${i}`] = i;
  const tool: SessionTool = {
    name: "Bash",
    input: "gh api graphql -f query='{}'",
    output: JSON.stringify(record),
    ok: true,
    archetype: "bash",
    summary: "$ gh api graphql",
  };
  const { container } = render(withUi(expandedUi(3), <ToolCallRow event={event(3, tool)} ticket="WIKI-274" withResult />));
  const body = container.querySelector(".session-tool-output-text")?.textContent ?? "";
  expect(body).toContain('{\n  "data"');
  const highlighted = container.querySelector(".session-tool-output-text .syntax-inline");
  expect(highlighted?.getAttribute("data-lang")).toBe("json");
});

test("ANSI-decorated read output keeps the ansi renderer (no highlighter)", () => {
  const esc = String.fromCharCode(0x1b);
  const ansi = [
    `${esc}[32mdeploy:${esc}[0m`,
    `${esc}[32m  stage: staging${esc}[0m`,
    `${esc}[32m  region: us-east-1${esc}[0m`,
  ];
  for (let i = 0; i < 12; i += 1) ansi.push(`${esc}[32m  service_${i}: null${esc}[0m`);
  const tool = codexReadTool({ output: ansi.join("\n") });
  const { container } = render(withUi(expandedUi(4), <ToolCallRow event={event(4, tool)} ticket="WIKI-274" withResult />));
  expect(container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});

test("prose without code shape stays plain (no false-positive highlight)", () => {
  const prose = Array.from({ length: 16 }, (_, i) => `note line ${i} — nothing structural here.`).join("\n");
  const tool: SessionTool = {
    name: "exec_command",
    input: "cat notes",
    output: prose,
    ok: true,
    archetype: "read",
    summary: "read notes",
  };
  const { container } = render(withUi(expandedUi(5), <ToolCallRow event={event(5, tool)} ticket="WIKI-274" withResult />));
  expect(container.querySelector(".session-tool-output-text .syntax-inline")).toBeNull();
});
