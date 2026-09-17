---
type: decision
tags: [zeta]
created: 2026-08-25
updated: 2026-08-25
---

# Zeta rendering philosophy


Decision (Henry 2026-08-25): zeta does NOT re-layout model output. Model prose renders with the model's own line structure verbatim — no block re-layout, no list restructuring, no managed prose re-wrapping (soft wrap at terminal edge only). Styling stays: syntax-highlighted code fences, inline emphasis, and all zeta-structural rendering (tool cards, receipts, notices, chrome) which keep the shared content width. Prose is exempt from the shared-width column.

**Why:** every layout bug in the 2026-08-25 polish arc (orphan gutter glyphs, separator anchor drift, loose-list double spacing, width stacking) came from re-layouting model text, not from styling it. Henry: "zeta shouldn't really need to handle rendering the model's output... for model outputs, I don't think we need any custom zeta rendering."

**How to apply:** any future TUI ticket touching prose rendering starts from verbatim-lines-plus-styling; re-layout proposals need an explicit Henry decision to reverse this. Implemented via the ZETA-SPACING lane (PR pending at decision time).


**SUPERSEDED (Henry 2026-08-25, same day):** after seeing Claude Code's rendering capabilities, Henry approved an intermediate markdown rendering layer (ZETA-MD lane): one real GFM parse per COMPLETED message (markdown-it-py) -> styled block tree -> paint at width; streaming shows plain text, replaced once on completion via identity-targeted units. The verbatim decision's real lessons carry forward as invariants: never parse line-by-line, never hand-roll markdown parsing, bounded-time rendering, literal-fallback on parse anomalies, theme transparency. Prose re-layout (wrapping at content width, tight lists, rendered tables) is back by design.
