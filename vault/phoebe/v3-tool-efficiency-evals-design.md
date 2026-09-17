---
type: reference
tags: [phoebe, v3-agent, evals]
created: 2026-09-10
updated: 2026-09-10
---

# V3 Tool Efficiency Evals Design


# v3 tool-efficiency evals on eval-engine (PHO-17482)

Decision (Henry, 2026-09-10): build v3 agent tool-efficiency evals on
github.com/phoebe-health/eval-engine, not a new harness. Sandbox LOAD
testing is explicitly out of scope for eval-engine (different tool, own
ticket later).

## Design (agreed shape)

1. Deterministic efficiency series from eval-engine's persisted
   per-sample `tool_calls`: tool-call count vs per-case par, bash bytes
   into context, repeated-identical commands, error-retry loops.
   Regression-checked against baselines (existing verdict + Slack path),
   NOT graded failures at first; promote stable pars later. Task outcome
   stays the pass/fail floor.
2. Binary judge panel gains tool-efficiency criteria (right-tool choice,
   no redundant re-execution, single-pass script). yes/no/unknown with
   honest coverage gaps, per existing scoring rules.
3. New flail-derived cases in `cases/v3_bash/` + `cases/v3_capabilities/`
   distilled from production admin-run audits; each carries an
   `Incident:` note (voice-canary precedent).

Boundary (eval-engine AGENTS.md): platform code never imports phoebe;
phoebe owns runners/graders. If a metric needs phoebe-side emission
(e.g. context bytes not derivable from tool_calls rows), flag as a
phoebe follow-up rather than crossing the boundary.

Related: [[v3-sandbox-s3-artifacts-implemented]] (sandbox history);
PHO-17446 (bash context byte budget) shares the bytes-into-context
metric.
