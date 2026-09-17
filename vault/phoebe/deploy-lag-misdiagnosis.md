---
type: til
tags: [phoebe, rca, deploys]
created: 2026-08-24
updated: 2026-08-24
---

# Deploy-lag misdiagnosis (PHO-16942)

A "still broken" complaint can mean the fix merged but did not deploy. PHO-16895 (#15154, inbound-SMS staff notifications default-on) merged Fri 2026-08-21 23:35Z; production did not deploy over the weekend. Heritage Senior Care's one caregiver text (Sat 16:45Z) hit worker release `8b4271e850` (pre-fix), so the org flag still read unset-as-off and the notification silently skipped. Team read this as the fix failing; ticket PHO-16942 filed Sunday. Monday's deploy carried the fix and unset-flag orgs began notifying.

## Recipe: prove which code processed an event

1. Find the event's trace in logfire; read `service_version` (a git SHA).
2. `git merge-base --is-ancestor <fix-sha> <service_version>` — exits 0 iff the fix was running.
3. Corroborate with data: for this path, `app.organization_notifications` rows with `dedupe_key LIKE 'inbound-sms-received:%'` per org.

## Gotchas surfaced

- Weekend merges to phoebe main can sit undeployed until Monday; complaints in the gap are expected, not regressions.
- `_notify_inbound_sms_received` (worker sms handler) and the `to_phone is None` drop in `notification_delivery` skip silently — no log states "notified N users / skipped because X". An observability PR was proposed and declined for now (2026-08-24).
- Heritage's flag is unset (nobody ever wrote `true`); default-on comes from the catalog, so "we force-enabled the ff" in the thread meant the code change, not an admin write.