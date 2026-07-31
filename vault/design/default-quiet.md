---
type: reference
tags: [design, doctrine]
created: 2026-07-30
updated: 2026-07-30
---

# Default-quiet UI doctrine

Henry, 2026-07-30: "we want the least amount of noise (the agent run preview + github diff elements are just pure noise off rip). ideally, we can OPT into noise (like clicking dropdowns) etc, but it shouldn't be showing by default."

## Rule

- Detail surfaces (run previews, diffs, diagnostics, chips, meta rows, long tool output) are OPT-IN: hidden by default, revealed by an explicit click (disclosure/dropdown/inspector).
- Default view shows only decision-relevant summary: identity, state, blockers, primary actions.
- Inner scrolling is a noise smell too — see [[WIKI-222]]: elements expand in the page flow; clamp+scroll only for genuinely large outputs (e.g. full file reads).

## How to apply

- Every new panel/preview in the wiki app ships collapsed-by-default with a disclosure, persistence optional per surface.
- Bake this into worker prompts for frontend tickets (done for WIKI-221, WIKI-218, WIKI-195).
- Complements [[polish-census]] and the "solid" north star: solid hierarchy = quiet by default, deliberate on demand.

## Instances

- WIKI-221: fleet-card screencast strip + working-diff panel behind disclosures.
- WIKI-222 (filed): kill nested scroll regions in stream output.
