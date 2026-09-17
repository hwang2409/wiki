---
type: til
tags: [zeta, agents]
created: 2026-09-03
updated: 2026-09-03
---

# Single Owner Concurrency Rule


## The evidence (2026-09-01..03)

Three review grinds in one repo, identical shape:

1. ZETA-56 MCP lifecycle: 4 rounds of races until `_transition` became the sole state owner.
2. ZETA-55 submission pipeline: 4 rounds (deadlock, slot overwrite, drops, cross-cancel) — point-fixes never terminated.
3. ZETA-58 MCP resilience: 3 rounds (6 -> 4 -> 5 findings); generation tokens threaded by hand kept leaking (object reuse, unversioned tool sets, untracked tasks).

Each round's specific findings got fixed; each round exposed a new race in the same subsystem. Reviewers dig one layer deeper every time — hand-threaded locks/tokens/callbacks always have another layer.

## The rule

- A concurrent subsystem gets ONE owner from the first implementation: an actor/queue-consumer task that exclusively owns the state; everything else sends messages. Locks, generation tokens, and deferred callbacks that touch state directly are review-round generators.
- Orchestrator kickoffs for concurrent features should mandate this shape UP FRONT (it was retrofit three times; never cheaper than doing it first).
- Escalation heuristic that worked: findings count bouncing up (not shrinking) across rounds in one subsystem = stop point-fixing, rewrite the ownership model. Applied to ZETA-55 (round 4) and ZETA-58 (round 3); Henry approved both rewrites 2026-09-03.
