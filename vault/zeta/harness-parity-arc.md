---
type: campaign
tags: [zeta, agents]
created: 2026-09-14
updated: 2026-09-14
---

# Zeta Harness-Parity Arc (2026-09-14)

Henry locked this arc on 2026-09-14 after a full feature-surface audit of zeta vs Claude Code / Codex CLI. Scope is EXACTLY four items (Henry: "That's it"): full skills system (incl. custom agent definitions), composer ergonomics, /init, image-capable reads. Explicitly NOT picked: native grep/glob tools, notifications/hook parity, IDE integration.

## Audit headline (2026-09-14 sweep)

Zeta is closer to parity than the deferred list implied. Already present: compaction, plan mode, shadow-git checkpoints/rewind, fork/branch tree, steering-default composer, 5-event hooks, sub-agents (cross-provider children), background tasks, MCP (OAuth+resources+prompts, `__` names), `@file` mentions (no autocomplete), todo UI, thinking display, headless `-p --format json`, custom slash commands (`.zeta/commands/*.md`, prompt+exec kinds — the old "macro DSL ZETA-53..55" idea is largely shipped). Automations landed via Henry's hand-merged #154.

Gaps chosen for this arc:

| Ticket | Lane | Content | Status |
|---|---|---|---|
| ZETA-117 | 1 of 5 | Full skills system (see spec below) | MERGED #160, 4 rounds |
| ZETA-118 | 2 of 5 | Custom agent definitions on shared discovery primitives | MERGED #164, 3 rounds |
| ZETA-119 | 3 of 5 | @file autocomplete + zsh/bash shell completion (live PTY tests) | MERGED #161, 3 rounds |
| ZETA-120 | 4 of 5 | /init generates or improves AGENTS.md | MERGED #162, 2 rounds |
| ZETA-121 | 5 of 5 | Image-capable read (6-row decision table, single WebP parser) | MERGED #159, 6 rounds |

## Constraints and context

- Ticket IDs 117-121 verified free on origin/main design.md (automations arc squatted 111..116, colliding with GUI-arc 111/112 — duplicate-ID rows are accepted repo precedent per Henry's #154 merge).
- Concurrency: spawns warned "9 active workers; soft cap 5" (phoebe fleet running 6). ZETA-119/120 spawn as slots free; watch for the 2026-08-24 EBADF worker-death class under load.
- Workers append their own design.md ladder rows in their PRs (arc: harness parity, lane N of 5).
- Prior arc thread: [[zeta-gui-wiki-run-parity]]; usage rationale: [[henry-daily-driver-profile]] (skills + screenshot reads + fewer prompts hit the daily-driver profile directly).
- UX-polish arc status: lane 2 (ZETA-112 image attachments, PR #158) still converging; lanes 3-4 specs died with the pre-wipe orch session — Henry pivoted to this arc instead, treat UX-polish 3-4 as dropped unless he revives them.

## Outcome (2026-09-14 ~20:53Z)

ARC COMPLETE — all five lanes plus ZETA-112 merged in one session; ZETA-122 (#163) repaired a ZETA-117 x ZETA-121 merge interaction (required ToolRegistry kwarg vs stale tests; 18-file module cap). Lessons captured in [[hot]] and mirrored here: post-merge combined suite when main moved; reviewers validate the virtual merge; impossibility-by-construction contracts for 2x-surviving finding classes; live PTY probes for shell artifacts. Deferred next-arc candidates: native grep/glob tools, notifications + hook parity, MCP QoL (ZETA-56..59).
