---
type: reference
tags: [phoebe]
created: 2026-07-09
updated: 2026-07-09
---

# Pr10475 Parity Loop


# PR-10475 Parity Iteration Protocol (Henry, 2026-07-09)

Standing protocol for the recommendation-subagent parity work on PR #10475. Worker copy at `/tmp/cdx-PR-10475-parity-loop.md`; orchestrator enforces.

## Goal
Subagents ON pass behavior within the OFF-vs-OFF noise floor (not pass-rate maxxing).

## Invariants
1. **OFF path frozen.** Branch touches shared recommendation files (recommend_caregivers_for_shifts.py, common.py, playbook_rules.py, callout.py, sidebar recommendation_tools.py) — flag-off behavior must be identical to origin/main; worker owes a file-by-file audit PR comment (gated / pure-refactor / OFF-behavior-change). Fixes may only touch subagent pipeline + eval harness. Never patch OFF to make parity easier.
2. **No overfitting.** No eval-case names/IDs/phrasing in product code; mechanism fixes only; orchestrator diff-scans every push for eval-string leakage.

## Loop (until parity goal met)
1. Test fix on failing cases first (currently memory-ledger: joshua_stewart_*_memory_qualified).
2. Full 54/54 baseline set.
3. Paired OFF vs ON re-eval.
4. Identify non-parity cases.
5. Find common trend/mechanism → next fix (one mechanism per iteration).
6. Implement, restart from 1.

Parity table + trend diagnosis posted as PR comment after every full run. Orchestrator monitors continuously and steers on drift.

## State (2026-07-09)
- Baseline 54/54 complete but contaminated (network blip: 12 infra_timeouts); serial subagents_off repair batch running (~5/11).
- Iteration-1 candidate mechanism: memory-ledger/exclusion evidence lost in parent-prefetch → compact-row → child pipeline (midway: wrong-picks 11 ON vs 1 OFF).
