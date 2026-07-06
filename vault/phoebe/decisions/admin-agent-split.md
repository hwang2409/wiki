---
type: decision
tags: [phoebe, admin-agent, architecture]
created: 2026-07-06
updated: 2026-07-06
---

# Admin Agent split: pure extraction first, own DB second

**Decided:** Phase 1 = move ALL admin-agent code out of `phoebe_event_agent` into `libraries/python/phoebe_admin_agent/`; General Agent plumbing and shared mechanics untouched — admin imports them via the barrel. Phase 2 = dedicated admin RDS (INTERNAL/SUBAGENT runs + admin_* tables; probe runs stay in app DB). ([PHO-13093](https://linear.app/phoebework/issue/PHO-13093), P0)
**Why:** readability + data isolation without an artificial repo-wide freeze; conflict surface = admin files only.
**Rejected:** full machinery copy per agent (subagent mechanics = ~20 tickets of race fixes, forking doubles every future fix); three-way maximal split as Phase 1 (forced repo-wide freeze for no immediate gain — deferred, not dead).
**Revisit if:** admin fan-in ever needs a different *algorithm* than general (full fork case), or barrel imports from phoebe_event_agent get unwieldy (agent_runtime extraction case).
