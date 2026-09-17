---
type: decision
tags: [phoebe, simulator, plan]
created: 2026-09-17
updated: 2026-09-17
---

# Simulator refactor plan (Orchard St. / phoebe-sim-daily)

Planned 2026-09-17 from the [[orchard-sim-org-audit-2026-09-17]] scout audit. Full evidence: [assets/eval-ui-arc/SIM-ORCHARD-SCOUT-memo.md](assets/eval-ui-arc/SIM-ORCHARD-SCOUT-memo.md).

## Locked decisions (Henry, 2026-09-17)

1. **Unify the demo audience.** Galiver demo outreaches must reach the 30 simulator caregivers. The seeded sim/demo partition (the 2-caregiver ceiling) is removed; only real-safety exclusions remain.
2. **Real Twilio, SMS-only.** Daily runs and demos keep the real Twilio sender for SMS. The drifted voice-escalation step is stripped from the callout template until an explicit simulator voice destination exists (none planned now).
3. **Pixel map deprioritized.** The Phaser world display stays as-is; investment goes to the operational event ledger and inspector. The world_scene split is an optional tail lane.
4. **Lock down both boundaries.** Public world display returns opaque labels and coarse status only; direct Worker admin routes require a signed service token.

## Tickets (filed 2026-09-17, sub-tickets of PHO-17587 "fix caregiver sim for demos")

Henry reordering: **demo-first, refactor after** ("prioritize making it just work for demos first").

- Demo-first: PHO-17588 (Galiver audience unify), PHO-17589 (daily-run RCA + heartbeat), PHO-17590 (telephony capability + strip voice escalation), PHO-17591 (event SMS transport).
- Refactor: PHO-17592 (filter unification), PHO-17593 (lexical voice removal), PHO-17594 (boundaries), PHO-17595 (persona manifest), PHO-17596 (seed ownership), PHO-17597 (workflow-run ledger), PHO-17598 (DO migrations/memory).

## Waves and lanes (original wave framing, superseded by the demo-first ordering above)

Sequenced waves; lanes within a wave may run in parallel (max 2 concurrent). Each lane is one PR-sized worker contract.

### Wave 0 — safety and correctness floor

- **L1 telephony capability record.** One explicit capability state: sender mode, assignment pool environment, webhook target, voice route, template mode. Strip the voice-escalation step from the callout template seed (decision 2). Replace the `staging-daily`-labeled-but-production-targeted assignment ambiguity with explicit environment metadata + validation. Read-only invariant report; daily execution refuses inconsistent state.
- **L2 unify outreach filter evaluation.** Migrate every callout/recommendation consumer to the shift-aware evaluator in `outreach_operations/exclusions.py` (honors per-client exception lists); quarantine/remove the exception-blind path in `outreach_filtering/filters.py`. Mixed-dataset regression tests (Galiver, non-Galiver, missing client, stale rule).

### Wave 1 — the demo-audience fix

- **L3 Galiver seed rework + cleanup.** Implement decision 1: rework `galiver_demo.py` filter-rule seeding so demo callouts reach the sim roster; make the 2 manual/demo caregivers a named policy rather than a tag inference. Archive the 52 inactive Galiver clients and stale past-open shifts. Verify against a live pending callout in production after deploy (read-only evidence).

### Wave 2 — voice-rule violations (critical repo-policy defects)

- **L4 remove lexical voice decisions.** Replace regex/word-set language detection (`tools/caregiver_sim/src/voice.ts:108-216`) and lexical no-call preference detection (`memory.ts:269-289`, `personality_agent.ts:2852-2867`) with structured provider/model signals and explicit preference events. Provider-backed evals covering paraphrases, partial turns, supported languages, live-human false positives. No new phrase lists.

### Wave 3 — reliability and observability

- **L5 daily-run RCA + heartbeat.** Root-cause why daily runs stopped 2026-09-02. Add heartbeat evidence: gate state, automatic setting, environment/org resolution, sender mode, last attempted/successful run, skip reason; alert when stale >1 local day.
- **L6 daily event transport contract.** Event SMS resolves the approved production Twilio sender through the same assignment/proxy path as normal outreach (mock bridge only in mock mode); record transport, target assignment, delivery evidence per run. Fixes `memory_event_missing_target`.

### Wave 4 — boundaries

- **L7 harden Worker + display boundaries.** Signed service token on all non-Twilio direct Worker routes; Twilio-webhook auth separated from admin auth; proxy target hosts restricted to approved URLs; request/turn/cost limits at the Worker boundary. Public world display payload reduced to opaque labels + coarse status; detailed payload stays behind the bearer token. Fix wildcard `postMessage` target.

### Wave 5 — structural (larger, sequenced after the floor is stable)

- **L8 versioned persona manifest.** One manifest generates the Python and TypeScript persona views (phones, ownership, environment, language capability, route target, seed version); fail closed on duplicates/environment mismatch.
- **L9 seed ownership + reconciliation.** Seed-owned markers on rows and memory fragments; reseed reconciles only owned rows; orphan report; dry-run diff before any live reseed.
- **L10 workflow-run ledger.** Link the Worker scenario harness to real product workflow records: inputs, model selections, memory IDs, outbox IDs, confirmations, outreach IDs, terminal state in one inspectable record. Largest lane; replaces pixel-map investment as the demo/inspection surface.
- **L11 Durable Object migrations + memory retention.** Numbered schema migrations, normalized evidence links, reflection dedupe, thread-scoped memory (cross-thread opt-in), bounded retention.

### Deferred / optional tail

- world_scene.ts presentation split (decision 3)
- deterministic run seeds for replay reproducibility (fold into L10 if cheap)

## Open product questions (not blocking wave 0-2)

- Which product workflow owns automatic refill after an SMS decline (daily event spec leaves it open) — surfaces during L6.
- Whether the pixel map is eventually retired outright once L10's ledger/inspector exists.
