---
type: reference
tags: [wiki-app, design]
created: 2026-08-06
updated: 2026-08-07
---

# OpenCode transcript doctrine

Extracted first-hand from https://github.com/anomalyco/opencode at packages/tui/src/routes/session/index.tsx (shallow clone at /tmp/opencode-ref — read the cited lines yourself). This is the design Henry wants Wiki's transcript to feel like. Adopt the DESIGN, not the harness.

## Provider parity target

- Claude rendering is the product benchmark for Codex agent runs.
- Normalize provider-specific events at the backend boundary into one canonical transcript model.
- Equivalent Claude and Codex messages, thoughts, tools, results, and errors use the same frontend components.
- Codex harness JavaScript must never reach the rendering layer. Convert each call into the same semantic tool shape Claude uses.
- Codex runtime cards prefer one inner `tools.*` call per `exec` script. Allow multi-call scripts only for parallel reads, retries, or local control flow; this improves transcript fidelity without removing useful agent behavior.
- Provider-specific UI is allowed only when the provider lacks equivalent source data. Show that limit directly.
- Encrypted Codex reasoning is one valid difference: render the fullest supplied summary, but never imply that Wiki can decrypt private reasoning.
- Parity tests must compare canonical events and rendered behavior across both providers. Do not build a second Codex rendering framework.

## The two-tier tool rendering model (core)

Every tool call renders as exactly ONE of:

### Tier 1 — InlineTool (the DEFAULT: one single line)
`index.tsx:1829-1985 (InlineTool / InlineToolRow)`

- Anatomy: fixed 2-char icon column + one line of text: `<icon> <Verb> <formatted-path-or-arg> <inline result metadata>`.
  - `$ git push origin main` (Shell inline form, :2090)
  - `→ Read frontend/src/session.tsx` (:2161-2169)
  - `← Edit styles.css (replaceAll)` (:2432-2436)
  - `✱ Grep "pattern" in src (3 matches)` (:2183-2194) — result counts fold INTO the same line, no result box at all.
  - sub-results as indented muted `↳ Loaded path` lines (:2170-2178)
- Per-tool icons are a micro-vocabulary: `$` bash, `→` read, `←` edit/write, `✱` glob/grep, `%` webfetch, `◈` websearch, `⚙` generic (:2090,2138,2162,2186,2198,2206,1808).
- State is expressed by COLOR of the same line, not extra rows (:1867-1874): normal text while running -> textMuted when complete -> error color if failed -> warning while awaiting permission -> denied = strikethrough (:1952). Completed tools literally recede.
- Pending (input still streaming): `~ Writing command...` placeholder or spinner in place of the line (:1946-1955).
- Failure: NO error box by default — click the row to toggle the error text expanded under it, indented by the icon width (:1978-1982).
- NO copy buttons, NO byte counts, NO header row, NO borders on tier 1. Ever.

### Tier 2 — BlockTool (the EXCEPTION: only for rich content)
`index.tsx:1987-2037 (BlockTool)`

Used ONLY when there is content worth a block: bash stdout (:2068-2088), edit diff (:2404-2431), write file contents (:2105-2120), task/subagent transcripts.

- Anatomy: LEFT BORDER ONLY (`border={["left"]}`, :2001), panel background shift (`backgroundPanel`, hover -> `backgroundMenu`, :2007), padding 1 top/bottom + 2 left, NO full box.
- Optional single muted title line `# Running in <dir>` / `← Edit <path>` (:2017-2030) — the title IS the header; no separate metadata row.
- Output caps: bash 10 lines (:2046), generic tools 3 lines (:1796); overflow = truncated + one muted `Click to expand` line (:2083-2085). No persistent controls.
- Errors append as one error-colored line inside the block (:2032-2034).
- Edit renders the actual DIFF (unified <120 cols, split above; syntax-highlighted, line numbers) — not an acknowledgement echo (:2393-2427).

## Density mechanics
`index.tsx:1932-1940 (setPreLayoutSiblingMargin), :95 (alwaysSeparate)`

- Margin between consecutive single-line tool rows = 0 — runs of reads/greps pack into a tight column, one line each.
- Margin 1 appears only when the neighbor is multi-line or marked always-separate (text parts, reasoning, blocks).
- Indent ladder: message content paddingLeft 3; icon column width 2; nested content indents by icon width.

## Reasoning/thinking
`index.tsx:1572-1677 (ReasoningPart / ReasoningHeader)`

- While streaming: spinner + `Thinking: <title>` in warning color.
- When done: collapses to ONE line: `Thought: <title> · <duration>` — warning hue at reduced opacity (`thinkingOpacity`), i.e. deliberately quieter than conclusions.
- Click to expand body; body renders as textMuted markdown, indented. Collapsed-by-default keeps layout from shifting.

## Global toggles (user-controlled density)
`index.tsx:256-261`

- `tool_details_visibility`: hide successfully-completed tool rows entirely (:1707-1711).
- `generic_tool_output_visibility`: generic tool output collapsed to inline form by default (:1806).
- These are session-level keybound toggles, not per-row chrome.

## Mapping to the six WIKI-245 defects

1. Fragmented call/DONE/result units -> tier model: ONE row (inline) or ONE block (title+content). Status is the row's color, never a separate "DONE" row.
2. Chrome dwarfing one-line results -> tier 1 has zero chrome; short results become inline metadata on the call line; only rich content earns a tier-2 block, and even that has one muted title line, a left border, and hover-only affordances.
3. Semantic mislabels -> per-tool renderers (Shell/Read/Edit/Write/Grep...) each know what their result IS; Edit shows a diff, acknowledgements collapse to color change. A "FILE CONTENTS" header over a status echo cannot happen in this model.
4. Harness wrappers -> tool errors render as the row's expandable error text (error color); system-reminders/boilerplate = fold into muted or strip at render (Wiki-specific; OpenCode has no equivalent noise — that's our own cleanup).
5. Scan hierarchy -> icon vocabulary + color state machine (active text / completed muted / failed error / permission warning) + verbs first. Paths formatted relative (usePathFormatter), not absolute.
6. Thinking -> the Thought one-liner with reduced-opacity warning hue + expandable muted body.

## Web-translation constraints (Wiki-specific, from orchestrator)

- Keep `--font-agent-prose` monospace; keep theme tokens; weights cap 600.
- Hover affordances must have keyboard/focus equivalents (WIKI-242 a11y pass is landing in parallel — don't regress it).
- Raw output access (WIKI-241) stays reachable — the tier system changes the default view, not data availability.
- "Click to expand" as muted text-hint is fine, but make it a real button semantically (focusable, aria-expanded).
