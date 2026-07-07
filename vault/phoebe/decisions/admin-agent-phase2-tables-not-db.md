---
type: decision
tags: [phoebe, admin-agent]
created: 2026-07-07
updated: 2026-07-07
---

# Admin Agent Phase 2: own tables, same DB

**Decided:** Phase 2 = admin-shaped run tables (`admin_agent_runs` + satellites) in the SAME Postgres, own schema shape — not a separate database instance.
**Why:** The real pain is schema misfit, not load/ops: admin runs are molded into org-scoped General-agent run tables — forced `organization_id` is why the "Phoebe Home Care" pinned-org hack exists (broke local dev twice, 2026-07). Dedicated tables kill the misfit; same-DB keeps run_id FKs (traces, artifacts, feedback, audit, saved investigations) working with zero dual-source consumers.
**Rejected:** Separate DB instance — every run_id-keyed consumer forks into dual-source or app-side joins; doubles ops surface (backup/migrate/seed/local stacks); the agent's read path stays on prod anyway, so load isolation is illusory. (Henry proposed, conceded after grilling 2026-07-07.)
**Revisit if:** admin write QPS becomes measurable vs prod, an admin migration blocks a customer deploy, or compliance demands separate retention for internal-tool data.
