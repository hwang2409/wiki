---
type: til
tags: [tools, wiki, agents, tests]
created: 2026-07-10
updated: 2026-07-10
---

# Wiki headless auto-resume can beat explicit /resume

**Symptom:** after killing a detached headless provider PID in acceptance tests, `POST /api/agents/<ticket>/resume` can fail `409 "run already has an attached provider adapter"`.
**Cause:** the supervisor's background recovery loop can exact-session resume the run before the explicit API resume request lands; both paths produce a healthy reattached worker.
**Fix:** tests and operator flows must treat either result as success: explicit `/resume` reattaches, or the daemon has already reattached and the current run is live again.
