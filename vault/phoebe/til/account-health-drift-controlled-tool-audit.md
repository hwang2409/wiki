---
type: til
tags: [phoebe, admin-agent, account-health]
created: 2026-07-31
updated: 2026-07-31
---

# Account-health drift requires controlled tool audit
**Symptom:** `Admin database query has no active audit event; refusing to execute without a trackable backend PID.` caused 542 failures across 271 organizations.
**Cause:** `account_health_drift_watch._load_rollup` called `get_admin_account_health_rollups` outside the controlled audit runner.
**Impact:** Both hourly cohorts failed before drift comparison or Slack delivery; completed success ratio was `0/542`.
**Fix:** Run the rollup through controlled execution. Preserve backend PID tracking and durable audit events.
**Test gap:** Drift tests mock `_load_rollup`; rollup tests discard `on_backend_pid`, so no test crosses the audit boundary.
**Source:** Logfire trace `019fb9277ab46ebdf15ec9dcd2ab65da`; PRs #12898 and #12986; `origin/main` `4f9aaed953`.
