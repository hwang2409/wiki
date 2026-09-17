---
type: reference
tags: [phoebe, qa, data]
created: 2026-09-16
updated: 2026-09-16
---

# Org clone-and-scrub for local QA


Design discussion with Henry 2026-09-16. Goal: clone one production
organization's data into a local database so outreach and general-agent
flows can be QA'd on realistic data.

## Locked decisions (Henry, 2026-09-16)

- **PHI stays.** No de-identification of names, addresses, notes, message
  history, or transcripts. Client PHI on the local machine is accepted.
- **The only requirement is zero side effects from local QA**: no texting
  or calling a real caregiver/client, no EHR (WellSky/AxisCare) writes, no
  other external egress.
- Retention/GC of clones: not designed yet; drop-and-reclone is the model.

## Design shape (proposed, not yet ticketed)

- Repeatable tool, not a one-off dump: export one org's row graph from the
  prod READ REPLICA, scrub, import into an isolated per-worktree local
  database (reuse the `--isolated` clone machinery's landing zone).
- Graph walk driven by the schema: org-scoped tables enumerable via the
  mode/RLS machinery; FK closure for the rest.
- **Scrub = neutralize egress, replace not delete**: phones -> well-formed
  unroutable synthetics (555 / Twilio magic numbers), emails -> synthetic,
  so flows still exercise end to end. Deleting would break the flows under
  test.
- **Fail-closed scrub manifest**: declarative per-column policy
  (keep / synthesize / null / exclude-table), reviewed like
  settings_audit_tables.yaml. An unlisted column FAILS the export, so a
  future PII/egress column cannot silently leak into clones.

## Egress classes to neutralize (the real hazard list)

1. SMS/voice: caregiver + client + coordinator phone numbers; org-level
   Twilio/RingCentral phone rows (real SIDs must not be cloned).
2. EHR writeback: vendor sync accounts and credentials — never cloned,
   sync disabled at import, unconditionally.
3. Email: destination addresses -> synthetic.
4. Slack / webhooks / automation destinations: org Slack config, webhook
   URLs -> nulled.
5. In-flight state: outbox rows, queued messages, active outreaches —
   EXCLUDED from the clone so local workers do not act on import.
   Optional second lock: import-time OutreachFilterRule skip-all entries.

## Safety rails

- Source must be the read replica (refuse other DSNs).
- Destination must be a local DSN (refuse otherwise).
- Watermark the cloned org name ("CLONE — <name> (QA)").
- Local periodic workers WILL act on realistic data (clock reminders,
  confirmations); channel scrubbing is the primary guarantee, in-flight
  exclusion the second.

## Open

- Which org to clone first (Henry's call at first run; tool takes --org).
- No Linear ticket yet.

## UPDATE 2026-09-16: Keegan already built most of this (PHO-12012)

Master ticket for the new work: PHO-17572.

Existing tool (Keegan, PR #9517 2026-06-18 + #11426 2026-07-16):
- `/admin/org-snapshots` page (apps/web/routes/_app/admin/org-snapshots.tsx)
  + `services/api/routes/admin/orgs/org_snapshots.py`: search prod orgs,
  start import jobs, list local snapshots. Dev-only via RunEnvironment;
  manages the prod readonly tunnel itself.
- `libraries/python/prod_org_snapshot` (importer.py + transforms.py):
  per-org import with deterministic UUID remap per snapshot key; phones
  rewritten to +1555xxxxxxx everywhere incl. deep JSON walk; FAIL-CLOSED
  phone audit (UnsafePhoneAuditError); vendor sync accounts + sync state
  excluded (no EHR writeback); org phone rows / RingCentral / Twilio A2P /
  external SMS providers / Slack / Stripe ids excluded or nulled; in-flight
  state excluded (outreaches, outbox, contact_attempts, shift_confirmations,
  voice_calls, sms_engine_runs, agent runs); all rows land mode=sandbox;
  org renamed "Prod Snapshot: <key>".

Gaps found vs the design above:
1. NO email scrubbing (zero email handling in the library) — email egress
   risk.
2. Exclusion is a skip-LIST, not a fail-closed manifest: new tables clone
   by default; new non-phone egress columns (webhook URLs, automation
   destinations) leak silently. Phone audit walk does catch new phone
   columns.
3. Bitrot risk: last substantive touch 2026-07-16; schema moved a lot
   since. Needs an end-to-end verification run.
4. Automation tables (user automations, PHO-17501) are NOT in the
   exclusion list — cloned automations could fire locally. Sweep needed.

Proposed phases for PHO-17572 (serialized lanes):
- P0 verify: run the existing importer on a small org end-to-end, catalog
  breakage.
- P1 gaps: email scrubbing + extend unsafe-value audit to emails; sweep
  tables/columns added since July for egress surfaces (automations!).
- P2 hardening (optional): flip skip-list to the fail-closed manifest.

## P0 verification run — 2026-09-16, PASSED with findings

Ran the existing importer end-to-end on prod org "A Better Solution in
Home Care" (130 cg / 126 sh; smallest non-empty), snapshot key
p0-verify-2026-09-16, via the local API admin endpoints.

Results:
- Import COMPLETED: 25 tables, 1175 rows, snapshot org
  f46b6cd2-4f86-5342-81ca-18c31500e572, all rows sandbox mode. No importer
  bitrot.
- Phone scrub VERIFIED on landed data: 148 phone contacts, zero outside
  +1555; phone safety audit passed.
- Email gap CONFIRMED live: 124 real caregiver emails (gmail/hotmail/
  icloud/yahoo/live) cloned verbatim.
- BROKEN: the route's built-in tunnel starter exits 127 under Bazel —
  _REPO_ROOT resolves into the runfiles tree where scripts/ is not
  packaged. Workaround: run
  `bash scripts/db_ops/access/connect_db.sh production --url` from the
  real repo root; it writes /tmp/phoebe-db-url-production-readonly and
  the importer proceeds.
- Open question for P1: no client tables landed (only caregiver-side);
  verify whether client records import at all or the source org lacks
  them.
- Skipped by importer this run: revenue_accounts.

P1 lane scope (PHO-17572): email scrubbing + extend fail-closed audit to
emails; automations exclusion/neutralization sweep for tables added since
July; fix the tunnel-starter path; answer the client-tables question.

## Henry decisions 2026-09-16 (later)

- Snapshot arc ships as ONE PR: P1 + P2 folded into PR #17233
  (fail-closed per-column manifest included; drift test vs schema.sql).
- Admin v3: NO automations surface until Henry manually tests core
  functionality (exists + works well) via /admin/v3-agent (PR #17230).
  Rung 10 stays parked; the earlier rung-10 product calls are moot until
  after his manual pass.
