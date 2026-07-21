---
type: decision
tags: [phoebe/decisions]
created: 2026-07-20
updated: 2026-07-20
---

# Agent harness: emulate Claude Code behavior


## Decision

When building the admin agent harness, emulate Claude Code's harness behavior as closely as possible (Henry, 2026-07-20). Claude Code is the reference implementation for context-efficient agent loops.

## The pattern (tool-output side)

- Tool outputs are hardcapped at the envelope layer — uniformly, not per-tool.
- Full output is written to a file (for us: sandbox file when live, else S3 artifact spill tier from [[PHO-13989|pho-13989]]).
- The truncated stub is self-describing: location, size, line count, shape hint, preview, and concrete slice commands (grep/jq/head) so the agent slices instead of re-reading.
- The read/slice path is itself capped, or the agent re-inflates context.

## Why

$38 run RCA (019f802a): context bloat compounds — large tool outputs re-read every turn and re-written into prompt cache on every 5-min TTL expiry (~$5.5 per 840k prefix). Bounding entry size bounds prefix growth for all tools at once.

## Cost arc tickets

- PHO-14087 — truncation breaker (output side)
- PHO-14088 — sandbox chunk-by-chunk authoring (output side)
- PHO-14090 — model tiering by run kind
- PHO-14094 — bounded tool-output envelope (input side, this decision's first concrete application)

Merge order gate: #11801 (PHO-14073 pulse revival) held until 14087/14088/14090 land.

## How to apply

Any future harness feature (subagents, compaction, retrieval, streaming) should first ask: "what does Claude Code do here?" and copy that shape unless there's a phoebe-specific reason not to.
