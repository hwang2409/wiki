---
type: til
tags: [phoebe, voice, til]
created: 2026-07-08
updated: 2026-07-08
---

# Voice QA Clock Writeback Action Family

**Symptom:** voice call says a caregiver clock-in/out was noted for WellSky, but `voice_call_audit_actions.result_json = {"reason": "action_family_disabled"}` and no `shift.clock_in.execute.v1` / `shift.clock_out.execute.v1` is published.
**Cause:** live voice call `audit_context.allowed_action_families` can include `shift_clock_writeback` from pending clock-shift context while post-call QA independently gates actions with `QA_VOICE_AGENT_ENABLED_ACTION_FAMILIES`; stale global config can silently leave clock writebacks `suggested`.
**Fix:** PHO-13144 / PR #10812 made `shift_clock_writeback` trust the persisted call-specific clock context instead of the generic QA family allowlist, while keeping caregiver/shift/direction/timezone checks.
