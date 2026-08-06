---
type: decision
tags: [wiki-app]
created: 2026-08-06
updated: 2026-08-06
---

# OpenCode design direction

Henry ruling 2026-08-06: Wiki's UI takes heavy design inspiration from the OpenCode TUI (github.com/anomalyco/opencode) — the DESIGN only, not the harness. He finds the TUI "really really good".

**Bar raised later same day: the agent run UI should be "almost exactly one to one" with OpenCode** (reference screenshot: assets/opencode-ui-reference.png — turn-header agent·model·duration meta rows, '+ Thought: Nms' rows, tinted user bars, packed tool runs, click-to-expand hints, no extra chrome). WIKI-249 tracks the systematic fidelity pass.

Orchestrator read the source first-hand (packages/tui/src/routes/session/index.tsx); the transcript doctrine with file:line citations lives at /tmp/WIKI-245-opencode-transcript-doctrine.md (promote here if /tmp rotates). Core extract:

- **Two-tier tool rendering**: tier 1 InlineTool = one packed line per call (2ch icon column, verb + relative path + inline result counts; state = color of the line: active text, complete muted, failed error, permission warning, denied strikethrough; click-to-expand errors). Tier 2 BlockTool = left-border-only panel with background shift, ONLY for rich content (diffs, bash output); line-capped (bash 10, generic 3) with muted click-to-expand hint. No copy buttons/byte counts/header rows on tier 1, ever.
- **Density**: zero margin between consecutive single-line tool rows; 1-line gaps only around multi-line content. Indent ladder: content padding 3, icon column 2.
- **Icon micro-vocabulary**: `$` bash, `→` read, `←` edit/write, `✱` glob/grep, `%` webfetch, `◈` websearch, `⚙` generic.
- **Thinking**: spinner "Thinking: title" while streaming; collapses to one quiet line "Thought: title · duration" at reduced-opacity warning hue; expandable muted markdown body.
- **User-level density toggles**, not per-row chrome (hide completed tools; generic output collapsed by default).

Applied: WIKI-245 (transcript) steered to this doctrine 2026-08-06; broader app-chrome adoption ticket to follow from the deep-research notes (dialog anatomy, footer, composer, motion). Web-translation constraints: monospace agent prose stays, theme tokens only, weights cap 600, hover affordances need focus equivalents, raw-output access preserved.