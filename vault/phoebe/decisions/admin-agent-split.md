---
type: decision
tags: [phoebe, admin-agent, architecture]
created: 2026-07-06
updated: 2026-07-06
---

# Admin Agent split: policy fork + shared mechanics, then own DB

**Decided:** split `phoebe_event_agent` three ways — `agent_runtime` (mechanics only), `phoebe_general_agent` (scoped subagent surface), `phoebe_admin_agent` (max surface) — then move admin runs/tables to a dedicated RDS instance ([PHO-13093](https://linear.app/phoebework/issue/PHO-13093), P0).
**Why:** readability + structural capability boundary; admin no-ceiling load off customer-hot tables.
**Rejected:** full machinery copy per agent — subagent mechanics absorbed ~20 tickets of race fixes (11849→12688); forking doubles every future fix. Subpackage-only reorg — boundary stays convention.
**Revisit if:** admin fan-in ever needs a *different algorithm* (not just different limits) than general — that's the case where a full fork wins.
