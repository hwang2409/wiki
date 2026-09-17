---
type: reference
tags: [phoebe, simulator, audit]
created: 2026-09-17
updated: 2026-09-17
---

# Orchard St. Homecare (phoebe-sim-daily) audit + simulator refactor input

Read-only production audit + full simulator code review by SIM-ORCHARD-SCOUT (2026-09-17), input for Henry's planned large simulator refactor. Full memo: [assets/eval-ui-arc/SIM-ORCHARD-SCOUT-memo.md](assets/eval-ui-arc/SIM-ORCHARD-SCOUT-memo.md).

## Headline findings

- Blacklist claim corrected: not 28/30. 32 active caregivers, ALL 32 carry conditional `OutreachFilterRule` SKIP rows (callout + recurring_shift), seeded by `caregiver_sim_seed/galiver_demo.py`. 30 simulator caregivers are allowed only for the 12 non-Galiver clients; the 2 manual/demo caregivers only for the 20 Galiver demo clients. Latest Galiver callout: 224 candidate rows -> 210 excluded, 14 allowed across 2 caregivers. Legacy `ShiftConfirmationSettings` lists are empty (not the cause).
- Daily runs STOPPED 2026-09-02 (82 completed runs since June 12; nothing after). `simulator_world_events` empty in production; world tick cron gate defaults off.
- Two CRITICAL voice defects violate the repo no-lexical-matching rule: language detection via word sets/regex (`tools/caregiver_sim/src/voice.ts:108-216`) and no-call preference via regex (`memory.ts:269-289`).
- Telephony drift risk: live Twilio sender is voice-enabled and the stored callout template still has a 1-min voice escalation step, but no active voice route exists. 20 `simulator_phone_assignments` labeled `staging-daily` target the production API.
- Two competing filter implementations: `outreach_operations/exclusions.py` honors per-client exception lists; `outreach_filtering/filters.py` does not — consumers can silently disagree.
- Boundary gaps: direct Worker routes (`/simulate`, memory writes, reset) lack app auth; public world display endpoint exposes persona + patient names unauthenticated.

Memo carries an 11-item prioritized refactor plan and 10 open product questions for Henry.

Related: [[render-ui-sandbox-split-brain]]
