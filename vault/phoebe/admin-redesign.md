---
type: reference
tags: [phoebe, design]
created: 2026-07-27
updated: 2026-07-27
---

# /admin full redesign (Henry, 2026-07-27)

Henry decision: complete redesign of every /admin/* page — scrap original styling, reimagine formatting, layouts, and page content ("previous efforts weren't enough", pages "sloppy"). One incredibly large PR, accepted as worth it.

## Design direction

- Taste target: **intersection of Ramp, Linear, and the wiki app** (Henry 2026-07-27).
- Reference material: Ramp screenshots in phoebe repo root (`ramp-cards-full.png`, `ramp-cards-scroll1-3.png`, `ramp-expense-scroll.png`, `ramp-products-fold1.png`, untracked); wiki frontend at `~/me/fun/wiki/frontend/`.
- Prior art being superseded: PHO-14461 A/B/C (#12292/#12295/#12353) purple design system, tokens, primitive kit.

## Sequencing (agreed 2026-07-27)

1. Land the in-flight admin PR set first: #12481 (invite follow-up), #12483 (conversation search), #12484 (tool discovery), #11475 (tpuf search tool), PHO-14459 Part B (chips). #12482 (dock scroll) merged 17:26Z.
2. Then cut the redesign branch — long-lived, one PR.

## Resolved decisions (Henry 2026-07-27)

- Q1: **scrap PHO-14461 primitives entirely** — complete reimagining, not an evolution of the purple system.
- Q3: **content/IA driven by information priority.** All important information stays in the product, but what an internal admin sees at any moment is scoped by signal tier: high-signal front and center; low-signal reachable in the UI but not in their face (progressive disclosure/drill-down). Doctrine anchor: [[laws-of-ux]] (lawsofux.com) — especially Cognitive Load, Selective Attention, Hick's Law/Choice Overload, Chunking, Pareto (vital-few metrics first), Tesler (absorb complexity in the system).

## Orchestrator plan sketch

- Phase 0 design census: walk every /admin/* page, classify each element/section into signal tiers (always-visible / one-click-away / buried-but-findable), catalog content sloppiness, propose per-page IA.
- Implementation: cc claude-fable-5 worker, /frontend-design + /make-interfaces-feel-better mandated, kickoff requires reading [[laws-of-ux]] + Ramp screenshots + wiki frontend, own worktree stack, screenshots per page.
- Vendored skill: `.claude/skills/laws-of-ux/SKILL.md` in phoebe repo (from github.com/lev-os/agents skills-db, vetted 2026-07-27) — procedural 21-law application guide; kickoff invokes it alongside the vault reference note.
