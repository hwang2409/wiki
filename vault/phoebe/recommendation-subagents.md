---
type: campaign
tags: [phoebe, evals]
created: 2026-07-06
updated: 2026-07-06
---

# Phoebe Recommendation Subagents — What Changed and What It Did

Campaign (June 30 – July 2, 2026): make General Agent recommendation
subagents faster and more correct than the direct no-subagent path. Result on
the tuned bank: **14/16 vs 6/16 passes, 2 vs 80 wrong drops, 0 wrong picks,
144s vs 410s, $21.51 vs $37.27** (fixed harness, 2 repetitions). A holdout of
8 untuned Agent Court cases then came back **1/8 with one forbidden pick** —
strong on the trained distribution, weak off it. The generalization fix arc
(three mechanism fixes, no tuning allowed) is in flight; see the holdout
section.

> The through-line: every win came from moving work out of LLM loops into
> deterministic code — prefetch the candidates, give children a bounded
> rank-task with no tools, fan results back as structured rows.

Metrics: *wrong picks (wp)* = suggested caregivers the answer key forbids
(safety); *wrong drops (wd)* = expected caregivers not suggested (coverage).

## The changes, and what each one did

| #   | Change                           | How                                                                                                                                                                                                                     | Effect                                                                                                                                                                               |
| --- | -------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Actually route to subagents      | Under `general_agent_recommendation_subagents_enabled`, the callout/outreach skill directs the parent to `recommend_caregivers_for_shifts`; direct recommendation tools removed from the parent surface                 | Before: flag on, but one 4-shift case still had the parent calling `get_recommendations` 15 times itself. After: subagents actually run. Biggest single fix of the campaign          |
| 2   | Prefetch-first, no-tool children | The tool assembles the candidate pool in code (engine pulls, roster profiles, org playbook) before any child spawns; children get compact `[row_index, flags]` rows, **no tools**, ~1 turn, return up to 20 ranked rows | Child work went from re-running the recommendation stack (~237s/case) to rank-only (~96–144s/case)                                                                                   |
| 3   | Trusted row→ID mapping           | Caregiver IDs never enter child context; a server-side map resolves row indexes at fan-in                                                                                                                               | Children structurally cannot invent IDs; also keeps instructions under the 3,900-char cap so more candidates fit                                                                     |
| 4   | Structured fan-in                | `child_completed` callbacks wake the idle parent; a completion normalizer maps rows→IDs/names; parent writes `add_outreach_suggestions` once, directly from child payloads                                              | Killed parent re-derivation and duplicate writes. Regression test: complete 1 of 2 children, assert no early parent wake                                                             |
| 5   | UUID preservation for child runs | `redact_unresolved_ids=False` on subagent child runs                                                                                                                                                                    | Pre-fix, child results came back as "the referenced item" and the parent re-ran recommendations to recover IDs. May be revertable now that children see row indexes — PR review item |
| 6   | Hard-drop vs soft-flag policy    | Pre-child exclusion only on strong evidence (structured `DO_NOT_SCHEDULE`, engine flags, playbook-gated rules like "cannot safely lift"); regex/tag heuristics only annotate                                            | wp=0 across all 48 on-arm repetitions (off-arm produced 3–10). Caveat: detector vocabulary is still global/bank-tuned — per-org extraction queued                                    |
| 7   | Coverage chain alignment         | Removed the top-4/5 child cap, the fan-in `up_to` backfill trim, and a normalizer bypass; classified backup/advisory rows separately from primaries; exact-count multi-target merge                                     | wd on the on-arm: 34 → 2. Each cap had silently traded coverage for compactness (top-4/5 alone caused wd=10; the bypass wrote 6 of 20 ranked rows)                                   |
| 8   | Shift-set grouping               | Similar ungrouped shifts merge into one child task; group tasks preserve exact `covered_shift_ids`                                                                                                                      | Fewer children per multi-shift outreach; exposed (then fixed) the grouped-prefetch `asyncpg` UUID crash that made children rank an empty pool                                        |
| 9   | Worker cap 6 → 16                | `GENERAL_SUBAGENT_ACTIVE_WORKER_CAP`                                                                                                                                                                                    | Real shift parallelism; phase-latency data later showed child LLM time (181s of a ~220s case), not queue wait, dominates — cap raise is a flagged standalone PR decision             |
| 10  | Fallback hardening               | Flag off → exact old path; prefetch empty/failed → clean degrade; child failure → explicit partial coverage. All regression-tested                                                                                      | Production-readiness; checkpointed separately                                                                                                                                        |

## Eval harness changes (needed before the numbers meant anything)

| Fix | Why it mattered |
| --- | --- |
| Scheduler pass-budget bug | Child streaming events consumed `max_process_passes=12`; the pump exited and **cancelled children mid-flight**. Every earlier on-arm number understated the arm. Fixed + deterministic children-run-concurrently test |
| Arm-config preservation | Seeded arm model override + reasoning effort were being dropped; report backfill made idempotent (5 commits) |
| `--parallel-concurrency` + repetitions | Full A/B = 2 reps x 2 arms x 8 cases at concurrency 16; keep/revert decisions only on repetition-backed aggregates |
| `phase_latency` instrumentation | Per-run breakdown (parent turns / queue wait / child LLM / fan-in) from persisted event timestamps — this is what showed child LLM time dominates |
| Known gaps (queued) | No case wall-clock cap (one off-arm case looped to the 200-iteration bound and held batches hostage twice); Braintrust upload retries block process exit; infra failures (a DNS outage produced a fake 0/4 smoke) must grade as infrastructure, not pick/drop |

Two rules held throughout: answer keys and graders were never edited (when a
case wrote 15 vs expected 13, the key was inspected first — verdict: real
backup-only classification gap), and global heuristics were never grown from
eval case phrasing after one violation got called out and reverted.

## Results

| Stage | On-arm | Off-arm | Note |
| --- | --- | --- | --- |
| First wiring | invoked, slower | — | harness was cancelling children |
| `compact13` — 1 rep, broken harness | 4/8 · 39 err · 237s · $10.93 | 3/8 · 50 err · 407s · $17.37 | first win; instrument-distorted |
| `compact14` — first fixed-harness 2x | 8/16 · wd 23 · 151s | — | keepable; coverage open |
| Fixed-harness A/B #2 | 8/16 · wp 0 · wd 34 · 134s · $20.66 | 6/16 · wp 3 · wd 90 · 663s · $56.97 | exposed prefetch crash + fan-in bypass |
| Peak A/B — Jul 2 (pre-cleanup) | 14/16 · wp 0 · wd 2 · 144s · $21.51 | 6/16 · wp 0 · wd 80 · 410s · $37.27 | partly earned by overfit vocabulary |
| **Honest bank — Jul 2 (PR line)** | **10/16 · wp 0 · wd 6 · 143s · $20.81** | 6/16 · wp 0-2 · wd 65-80 · 345-410s · $27-37 | post-integrity-cleanup, stable+explained |
| Agent Court holdout — Jul 2 | 0/8 · 3 forbidden · 176s · $9.06 | 1/8 · 1 forbidden · 440s · $17.32 | both arms fail untuned cases; parity within noise |
| Heavy cases, post-10442 bank — Jul 2 | 6/12 · 2 unsafe · wd 2 · 273s · $17.56 | 6/12 · 1 unsafe · wd 7 · 440s · $23.03 | 1 rep; new YAML case bank |
| **Generated bank, PR candidate — Jul 3** | **2/20 · 6 forbidden · 24 missing · 141s · $18.29** | **11/18 · 0 forbidden · 9+ missing · 399s · $36.43** | 2 reps, pinned `a502fb3f95`, clean infra; off-arm wins correctness+safety decisively |

The Jul 3 result hardened the campaign's core finding into an asymmetry
thesis: **the subagent architecture is brittle-fast, the direct path is
robust-slow.** When the prefetch supply lines (playbook rules, memory
qualification, language context) deliver, subagents beat the baseline on
every axis; when a supply line misses for a case shape, children rank blind
and fail harder than the self-sufficient direct agent — the off-arm's 2/2
on memory-qualification cases proves the data is retrievable inline while
the pipeline fails to carry it into rows. Any production decision must
price this: speed/cost are unconditional, correctness is conditional on
supply-line completeness per case distribution.

Notes on reading it: the off-arm baseline is worse than assumed — the strict
keys fail the shipped product too. Off-arm latency means carry a heavy tail
(re-query loops + repeated compaction); report medians alongside means.

### The holdout reality check

PR #10343 added 10 Agent Court-generated demyst cases (8 runnable) to main
mid-campaign — cases nobody tuned toward. Run once, on `claude-opus-4-8`, at
the PR-candidate commit: **1/8**. The failures decompose into three
mechanism classes, not eight case-specific bugs:

1. **Missing attributes.** The cases test language match, prior declines,
   inactive-caregiver recovery, overtime — signals that exist in product
   data but never reach child rows. The same caregivers (Castaneda,
   Gallegos, Caldwell, Cole) go missing across five cases because children
   cannot judge what they cannot see. Prefetch schema gap, not ranking.
2. **Hardcoded exclusion vocabulary.** One case failed with "suggested
   forbidden caregivers: Jamie Caldwell" — a wrong pick. The bank-tuned
   detector vocabulary missed an exclusion phrased differently. The
   zero-wrong-picks record was bank-specific.
3. **Bank-shaped count doctrine.** The tuned bank's keys rewarded
   "everything eligible up to 20"; the holdout penalizes padding
   ("expected exactly 6, got 13" plus 11 padding names). Count policy was
   learned from the bank's grading philosophy — it must derive from the
   org playbook/request context, with children distinguishing qualified
   from padding and stopping at the qualified pool.

The fix arc (steered, in flight) is mechanism-level with anti-overfit
protocol: every new signal must name its org-derived data source; changes
referencing eval caregivers/cases/phrasing are rejected; holdout reruns
budgeted at 2 for the whole arc; final validation adds a freshly generated
sealed case set whose keys are never inspected. Acceptance: original bank at
or above the current line, holdout materially improved, zero forbidden picks
across all three sets.

## What's left

- [ ] **Generalization mechanisms (in flight, blocker)** — (1) plumb
  structured attributes into child rows: languages, active status, decline
  history, exclusion records, overtime; (2) per-org rule extraction: cached
  typed `OrgRecommendationRuleSet` from each org's playbook replaces global
  regexes as the only hard-drop source, plus the leakage audit (delete any
  constant encoding answer-key-only knowledge); (3) playbook-derived count
  policy with an explicit qualified-vs-padding child output. Plan:
  `.agents/plans/recommendation-subagent-generalization.md` in the worktree.
- [ ] **Three-set validation** — original bank ≥ current line; holdout
  rerun (2-run budget); fresh sealed generator cases. Zero forbidden picks
  everywhere or no PR.
- [ ] **T1 stable defect** — misses Gary Fernandez / includes Jennifer Young
  in both reps; ship documented.
- [x] **PRs split and shipped** — product PR #10475 (open, green, review
  fixes landed: translator task-kind gating, per-batch fan-in scoping,
  cap ratchet removed, marker-based prompt splicing, overfit-residue
  deletion); harness PR #10480 **merged 2026-07-03** (scheduler
  cancellation fix, exception-as-result, arm-config preservation,
  instrumentation — Demyst subagent-arm evals on main are now faithful).
- [ ] **Rollout** — staging QA replay → internal org dogfood → 1–2 friendly
  orgs → broader. Customer orgs gate on zero wrong picks across all sets.
- Untried latency lever: cheaper child models + escalation (child LLM time
  dominates the phase breakdown).

## Pointers

| What | Where |
| --- | --- |
| **Product PR** | **#10475 "Add org-derived recommendation subagents"** (branch `subagent-recommendation-generalization`; harness PR held separately) |
| Worktree | `~/me/fun/phoebe-pr10234-subagent-evals` (`codex/pr10234-subagent-evals` = integration branch, coworker-shared) |
| 10442 eval worktree | `~/me/fun/phoebe-pr10442-subagent-evals` (stack rebased onto Keegan's YAML case bank) |
| Checkpoints | compact14 · exact-count fan-in · fallback hardening · safe-core smoke · fixed-harness A/B · `7442b826e1` holdout baseline · `3b2e2fbe01` generalization plan (SHAs pre-rebase differ; `git log --oneline --grep=checkpoint` in the worktree is authoritative) |
| Goal session | Codex thread `019f157b-5169…` (`~/.codex/goals_1.sqlite`) |
| Eval bank / runner | `evals/demyst_eval_bank.py`, `evals/runners/seeded_org_callout_runner.py` |
