---
type: reference
tags: [phoebe]
created: 2026-07-09
updated: 2026-07-10
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

## Codex successor iteration (2026-07-09)
- Step 0 smoke on `058f620` used `demyst/generated/joshua_stewart_top_5_memory_qualified`; OFF used direct `get_recommendations`, ON spawned recommendation subagent child run `019f48ef-86b5-7555-8f81-05b6ed51107b`; PR smoke comment posted.
- Step 1 full paired run on `058f620`: `output/voice-eval-batch/pr10475-current-r2-20260709T222132Z-058f620/`, 27 paired base cases x 2 reps = 54 ON/OFF pairs. Current OFF/ON: mean overlap `0.056`, exact-set `0.056`, verdict agreement `0.778`, one-sided forbidden picks `10`. Current OFF/OFF self-noise: mean/exact `0.074`, verdict agreement `0.963`, one-sided forbidden picks `0`. Status remains below noise floor.
- Step 2 OFF-worse-than-ON set by verdict flip and exact-set criteria: `demyst/generated/joshua_stewart_top_5_memory_qualified`, `demyst/generated/joshua_stewart_ask_10_stop_at_6_memory_qualified`, `demyst/generated/callout_minimum_suggestions_language_underfill`. Same mechanism hypothesis still strongest: parent-prefetch/compact-row child input loses required memory/exclusion evidence before subagent ranking.

## Codex mechanism-fix iteration (2026-07-10)
- Pushed `3a22dfe360` to PR branch: `Carry target requirement evidence through recommendation subagents`. Mechanism: parent prefetch/compact child rows now preserve hard target requirements, source-outreach context, CareFinder required patient tags, required-language pools, and exclusion evidence so ON child ranking sees the same qualifying evidence as OFF direct recommendations.
- Targeted Step 4 rerun passed prior divergers in `output/voice-eval-batch/pr10475-step4w-dirty-20260710T102738Z-058f62081b/`: `joshua_stewart_top_5_memory_qualified`, `joshua_stewart_ask_10_stop_at_6_memory_qualified`, `callout_minimum_suggestions_language_underfill`.
- Full paired run on `3a22dfe360`: `output/voice-eval-batch/pr10475-step1-postfix-20260710T104452Z-3a22dfe360/`, parity JSON `parity-report.json`. Comparator paired 27 ON/OFF cases from 56 eval arms; two unsuffixed harness cases were not paired.
- Current OFF/ON comparator: mean overlap `0.037`, exact-set `0.037`, verdict agreement `0.889`, one-sided forbidden picks `1`. Baseline `05c50f2` was `0.037` / `0.037` / `0.815` / `8`; improved but not parity.
- Remaining ON-worse correctness cases: `demyst/generated/joshua_stewart_ask_10_stop_at_6_memory_qualified` (ON returned 8 vs OFF exact 6) and `demyst/generated/outreach_required_c3_criterion_smaller_than_count_floor` (OFF passed, ON produced 0 suggestions). Remaining ON-only forbidden-pick pair: `demyst/joshua_stewart_qualified_pool`.
- PR iteration comment posted: https://github.com/phoebe-health/phoebe/pull/10475#issuecomment-4934977061.
