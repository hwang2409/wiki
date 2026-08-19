---
type: reference
tags: [phoebe]
created: 2026-08-10
updated: 2026-08-10
---

# v3 workflow census & golden evals (2026-08-07)


Production workflow census run 2026-08-07 by worker PHO-0-V3-WORKFLOW-CENSUS1
(read-only production tunnel; PII-free by construction). Purpose: understand
the primary workflows customers use the agent for, and derive golden eval
cases for v3.

## Files (preserved from /tmp)

All three live in `assets/v3-workflow-census/`:

- `report.txt` — human-readable census report (renamed from report.md; vault lint treats .md as notes)
- `workflow-census.json` — machine-readable taxonomy, full write-tool audit,
  frequency-weighted replay cases + synthetic-seed requirements
- `golden_eval_cases.json` — 30 golden eval cases, one per distinct scheduler
  workflow type (merge-only-semantic-duplicates rule)

## Key numbers

- Window: 2026-07-24 → 2026-08-08 (14 days), 7,821 eligible production runs
  (live general chat + outreach analysis; internal/sandbox/automation excluded).
- **83.5% of all runs are outreach recommendations + monitoring** (6,527).
  Next: shift lookup/scheduling 433, caregiver lookup/memory 218, outreach
  drafting 166, client lookup 162, activity/alerts 103, docs/reports 80,
  EHR sync 41, messaging 21.
- Dominant real sequence: `get_recommendations` (xN) →
  `add_outreach_suggestions` (1,118 runs on the top variant alone) — the
  same multi-shift flow that motivated the registers design ([[PHO-15392]]
  context in phoebe repo, PR #13744).
- Top write tools by volume: add_outreach_suggestions (6,818 calls),
  manage_outreach_add_contact_attempts (3,469, high risk),
  send_one_off_sms_in_conversation (2,287, high risk). Suggestion add/remove
  and outreach-status writes showed ZERO pending-approval states — confirm
  policy intent.

## v3 gap analysis (from the census)

- v3 read surface covers only basic caregiver/client/shift retrieval.
- Missing context: outreach records, contact attempts, recommendation state,
  conversation history, activity events, callouts, monitoring, EHR state,
  playbooks, settings, reports.
- Missing writes: every agency mutation and every customer communication.
- Effectively a volume-ranked roadmap for v3 tool buildout.

## Status / next steps

- Golden REPLAY was attempted 2026-08-07 (worker PHO-0-V3-GOLDEN-REPLAY1) but
  provider credentials were missing — model/tool behavior never exercised.
  Replay artifacts at /tmp/v3-golden-replay* (ephemeral). Re-run pending.
- Replay must use synthetic seed data only (seed dimensions listed in
  report.txt); never production names/numbers/messages.
