---
type: reference
tags: [phoebe, evals]
created: 2026-09-17
updated: 2026-09-17
---

# Eval adherence checklist (howtoeval.com + Anthropic demystifying-evals)

Canonical checklist for every phoebe eval family (v3-evals and beyond), distilled 2026-09-17 from howtoeval.com and anthropic.com/engineering/demystifying-evals-for-ai-agents. Reviewers audit against this; kickoffs cite it.

## Task design

- [ ] Two domain experts would reach the same pass/fail verdict; everything graders check is stated in the task/prompt.
- [ ] REFERENCE SOLUTION per case: a known-good trajectory/evidence fixture that passes all checks — proves the task solvable and the grader configured right (complement to adversarial fixtures, which prove bad trajectories fail).
- [ ] Positive AND negative behavior cases: for every "agent must do X" case, a sibling where X is wrong (e.g. a question that should NOT render a table; a task where bash is the wrong tool). Class balance prevents one-sided optimization.
- [ ] Each case labeled capability (expected to fail today; hill to climb) vs regression (must stay ~100%); golden promotion is for regression cases.

## Grading

- [ ] Deterministic graders wherever possible; grade outcomes and persisted state, never paths, step sequences, or lexical command/prose matching.
- [ ] LLM judges: failure-only diagnosis, never verdict-overriding; always given an "unknown" escape hatch; one dimension per judge call.
- [ ] Bounds/pars documented with derivation and capable of failing.
- [ ] Partial visibility: named per-check outcomes so failures are interpretable.

## Trials and verdicts

- [ ] pass^k (all trials pass) is the reliability metric for user-facing floors; pass@k is for capability hills. (Richard's harness computes both.)
- [ ] 0% pass across many trials = suspect a BROKEN TASK first, not an incapable agent; verify with the reference solution before recording it as agent signal.
- [ ] Regression-floor promotion needs 3+ trials of evidence, not 1.

## Environment

- [ ] Clean isolated environment per trial (v3-evals throwaway orgs satisfy this); no cross-trial state, no artifacts from prior trials readable.
- [ ] Eval harness matches production agent path (code-aware; no prompt-only scoring).

## Maintenance

- [ ] Read failing-trial transcripts before recording results; a failure must be FAIR (clear what the agent got wrong).
- [ ] Golden cases carry an Incident/Rationale note naming what they protect against.
- [ ] Prune ruthlessly: high-signal small sets; drop cases with no failures in ~3 months; watch saturation (100% = regression-only signal).
- [ ] Ownership named; the loop (production failure -> repro -> golden case) stays alive.

Related: [[PHO-17602-SCOUT-memo]], [[PHO-17602-CENSUS-memo]], docs/testing.md.
