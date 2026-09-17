---
type: reference
tags: [phoebe, evals, memo]
created: 2026-09-17
updated: 2026-09-17
---

# SIM-ORCHARD-SCOUT memo

Date: 2026-09-17

Scope: read-only production audit and full simulator code review.

Production queries used the repository `bin/query-db production` workflow.
The session used the `readonly` role on the analytics replica. No writes,
outreach, SMS, calls, runs, or local services were started.

## Executive summary

- The `28 of 30` blacklist claim is stale. Production has 32 active caregivers.
- All 32 have conditional caregiver `SKIP` rules for callout and recurring shift.
- The latest seven-shift pending callout had 224 candidate rows.
- The rules excluded 210 rows and left 14 rows for two manual/demo caregivers.
- The legacy shift-confirmation lists are empty. They do not cause the cap.
- The cap comes from conditional `OutreachFilterRule` rows.
- Simulator caregivers are not globally blocked. They are allowed for 12
  non-Galiver clients. Manual/demo caregivers are allowed for 20 Galiver
  clients.
- Production uses one live Twilio SMS sender and 20 active simulator phone
  assignments. The sender has voice enabled.
- No active voice routing destination exists. Seed code therefore considers
  daily voice dispatch disabled.
- The stored callout template still contains SMS plus one-minute voice
  escalation. This does not match the current seed expectation.
- The latest daily run is September 2. No daily run exists after that date.
- The simulator has two hard-rule voice defects. It uses lexical speech
  language detection and lexical no-call preference detection.
- The daily event SMS path only selects a mock sender. It cannot target the
  current production Twilio sender.

## 1. production audit

### organization and mode

| field | observed value |
| --- | --- |
| name | Orchard St. Homecare |
| internal name | `phoebe-sim-daily` |
| id | `587fad6f-8c19-4789-bfd5-0110d707e808` |
| timezone | `America/New_York` |
| live caregivers | 32 |
| sandbox caregivers | 5 |
| live clients | 84 |
| sandbox clients | 24 |
| live shifts | 4,308 |
| sandbox shifts | 73 |
| live filter rules | 65 |
| live outreaches | 2,167 |
| sandbox outreaches | 10 |

The organization is marked as an internal Phoebe organization and training
mode. Its feature set enables caregiver simulation, outreach sending, agent v3,
voice assignment, survey outreach, and inbound SMS notifications. Urgent
outreach and generic phone forwarding are disabled.

The production row has no `voip_provider` or `demo_config`. It has one live
organization phone row. That row is SMS-enabled, voice-enabled, and Twilio
backed. The phone number is omitted from this memo.

### blacklist mechanism

The current live filter rows are:

| entity type | outreach type | mode | rows | exception behavior |
| --- | --- | --- | ---:| --- |
| caregiver | `callout` | `skip` | 32 | each row has a client exception list |
| caregiver | `recurring_shift` | `skip` | 32 | same caregiver/client lane split |
| client | `callout` | `skip` | 1 | one client-specific skip |
| any | any | `allow` | 0 | no allow list |

The 32 caregiver rows cover every active caregiver. Thirty caregivers carry
the exact `simulator` preference tag. Two active caregivers have no simulator
tag and are the manual/demo records.

The exception lists explain the apparent two-caregiver ceiling:

- the 30 simulator caregivers are skipped unless the client is one of the 12
  active non-Galiver simulator clients;
- the two manual/demo caregivers are skipped unless the client is one of the
  20 active Galiver demo clients.

The aggregate audit used this shape of query. It did not select phone numbers
or message bodies.

```sql
SELECT entity_type, outreach_type, filter_mode, count(*)
FROM outreach_filter_rules
WHERE organization_id = :organization_id
  AND mode = 'live'
GROUP BY entity_type, outreach_type, filter_mode
ORDER BY entity_type, outreach_type, filter_mode;
```

The runtime decision is per caregiver and shift. A conditional caregiver skip
is not the same as an unconditional blacklist.

```python
def excluded_for_shift(rule, caregiver_id, shift_client_id):
    if rule.allow_clients and shift_client_id not in rule.allow_clients:
        return True
    if shift_client_id in rule.skip_clients:
        return True
    if caregiver_id in rule.unconditional_caregivers:
        return True
    exceptions = rule.caregiver_exceptions.get(caregiver_id)
    return exceptions is not None and shift_client_id not in exceptions
```

The latest pending callout was created on September 16. It covered seven
shifts and produced 224 candidate rows:

| caregiver class | client class | excluded rows | allowed rows | caregivers |
| --- | --- | ---: | ---: | ---: |
| simulator | Galiver demo | 210 | 0 | 30 |
| manual/demo | Galiver demo | 0 | 14 | 2 |

Therefore, the exact current result is 30 excluded caregivers and two allowed
caregivers for those Galiver shifts. The phrase `28 of 30` is incorrect.

The second pending callout, created earlier on September 16, has one shift and
zero candidate rows. This is a stale or incomplete pending record.

The legacy `ShiftConfirmationSettings` row has `enabled=true`,
`caregiver_filter_mode=skip_list`, and empty skipped and allowed caregiver
arrays. It has no effect on the observed cap.

The seed implementation explains these rows. It is in
`libraries/python/caregiver_sim_seed/galiver_demo.py:478-600`.
It creates conditional caregiver skip rules for active caregivers. The runtime
callout evaluator is in
`libraries/python/outreach_operations/exclusions.py:35-123`.
The older generic filter implementation in
`libraries/python/outreach_filtering/filters.py:86-146` does not apply the
exception lists. This split is a correctness risk. Every callout consumer
must use the per-shift evaluator.

### clients and shifts

Live client counts are:

| client class | active | inactive |
| --- | ---: | ---: |
| Galiver demo | 20 | 52 |
| non-Galiver | 12 | 0 |

The 52 inactive Galiver clients are legacy accumulation. The seed expects a
20-client active Galiver roster with a rolling seven-day horizon and 14-day
retention. The inactive rows are not part of the current candidate cap, but
they increase audit noise.

Live shift status counts are:

| client class | assigned | confirmed | completed | in progress | missing | open | pending |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Galiver demo | 1 | 0 | 0 | 0 | 0 | 399 | 0 |
| non-Galiver | 449 | 168 | 402 | 172 | 413 | 1,091 | 459 |

There are also 754 cancelled rows tied to Galiver or no-client markers.
At the audit time, 138 Galiver marker shifts were future open shifts.
Another 261 Galiver marker shifts were past open shifts. The non-Galiver
population has 1,091 past open shifts. These old open rows suggest that
generic simulator shifts are not being retired with the same discipline as
the Galiver rolling seed.

### outreach and message activity

Live outreach status counts are:

| outreach type | status | count | latest observed |
| --- | --- | ---: | --- |
| callout | pending | 2 | Sep 16 |
| callout | cancelled | 12 | Sep 15 |
| callout | unfilled | 91 | Sep 11 |
| callout | filled | 1 | Jun 29 |
| clock-in | filled / unfilled | 282 / 589 | Sep 6 / Sep 5 |
| clock-out | filled / unfilled | 405 / 783 | Sep 6 / Sep 6 |
| recurring shift | unfilled | 1 | Aug 14 |

The live message table has 17,172 rows in the last 90 days. It has 7,024
inbound rows, 10,148 outbound rows, and 934 failed rows. The last message is
September 15. The last 14-day window has 168 messages and 11 failed rows.

There are 87 live idle conversations. Fifteen had activity in the last 14
days. No live conversation was active at the audit time.

Callout events show response activity through September 10. They contain
2,082 response events, 1,602 initiation events, 14 filled events, and one
deleted event. The gap between this activity and the September 15 message
date needs operational review.

### daily runs and world state

Production has 82 completed daily runs and two failed runs. The run dates span
June 12 through September 2. There are no runs after September 2.

Across the live runs, the stored totals are:

| metric | total |
| --- | ---: |
| attempted shifts | 1,082 |
| filled | 361 |
| unfilled | 285 |
| needs review | 0 |
| SMS | 1,994 |
| voice | 129 |
| proxy failures | 0 |
| Twilio failures | 101 |

The production `simulator_world_events` table is empty. The world tick path
has not recorded a production event. The worker cron exists, but its automatic
setting defaults to false in
`services/worker/handlers/system/simulator_world_tick.py:1640-1705`.

### phone and voice configuration

Production has 20 active `simulator_phone_assignments` rows. Every row has:

- live mode;
- environment `staging-daily`;
- target base URL `https://api.phoebe.work`;
- a Twilio SID;
- no update after its July 10 creation.

The `staging-daily` label paired with a production API target is confusing.
It may be an intentional shared pool, but it needs an explicit environment
contract before the refactor.

The organization has voice settings, and its sender phone is voice-enabled.
However, it has no active voice routing destination. The seed helper therefore
returns false from
`libraries/python/caregiver_sim_seed/daily_simulator_sms.py:222-253`.

The stored callout outreach template still has an SMS step at delay zero and a
voice step at delay one minute. The seed would force SMS-only callout behavior
when no voice dispatch route exists, in
`libraries/python/caregiver_sim_seed/outreach_templates.py:27-69`.
The stored template is therefore drifted from the current configuration.

The stored clock-in and clock-out templates are SMS-only. That part matches
the seed expectation.

The daily sender implementation deliberately provisions a real Twilio sender
when both production settings are present. See
`libraries/python/caregiver_sim_seed/daily_simulator_sms.py:20-95`.
The README still describes current staging daily runs as mock SMS. Production
does not match that description.

## 2. simulator architecture map

### seed and production records

`libraries/python/caregiver_sim_seed/seed_data.py:69-240` coordinates the
standard and daily seeds. It writes normal Phoebe caregivers, clients,
availability, preferences, memories, shifts, outreach templates, and runtime
settings.

`caregiver_records.py` matches caregivers by phone or external snapshot. It
stores simulator identity in `ExternalCaregiverSnapshot.vendor_data` while
using `WELLSKY` as the source-system value. This gives simulator records an
EHR-shaped identity that can collide with future integration assumptions.

`caregiver_details.py` refreshes availability, preferences, and memory. It
updates rows marked as manual availability and compiles seeded memory. It does
not clearly separate seed-owned memory fragments from product-extracted
fragments.

`galiver_demo.py` owns the 20-client roster, rolling shifts, inactive cleanup,
and the conditional filter rules. It is the source of the observed blacklist
state.

`daily_simulator_phone_pool.py` resolves 20 live phone slots. It parses slot
numbers from `friendly_name` and uses last-row-wins behavior for duplicate
slots. It validates completeness only when no mock sender exists.

### caregiver simulator worker

`tools/caregiver_sim/src/index.ts:1025-1630` is the public Worker router. It
serves the UI, simulation endpoints, memory endpoints, logs, reset, direct
Twilio webhooks, and proxy webhooks.

`tools/caregiver_sim/src/personalities.ts` owns the standard persona catalog
and the daily persona mapping. The Python seed persona definitions and the
Worker definitions are separate sources. They can drift.

`tools/caregiver_sim/src/personality_agent.ts:273-412` implements one
Cloudflare Durable Object per personality phone. The object creates SQLite
tables for messages, decisions, summaries, memory events, and scenario runs.

`tools/caregiver_sim/src/memory.ts` provides keyword extraction, recency,
relevance, importance scoring, and reflections. It also contains the
forbidden lexical communication-preference detector.

`tools/caregiver_sim/src/workflows.ts` builds the workflow prompt and parses
workflow types and channels. The Worker scenario loop is an admin harness. It
does not always enter the same Phoebe workflow path as a real outreach.

`tools/caregiver_sim/src/voice.ts` builds TwiML and selects speech language.
The language decision currently uses word sets and regular expressions.

`tools/caregiver_sim/src/twilio.ts` and `twilio_credentials.ts` send replies
and select credentials. `twilio_validation.ts` validates direct Twilio
requests. The direct Worker routes remain reachable on the Worker domain.

### API proxy and admin routes

The current API route is
`services/api/routes/admin/caregiver_sim/caregiver_sim.py`, not the older flat
path named in the task surface map. The API mounts it under
`/admin/caregiver-sim` in `services/api/routes/admin/__init__.py:296-362`.
The parent router requires an authenticated admin and applies an admin rate
limit.

The route proxies Worker calls, normalizes payloads, and exposes seed, logs,
scenario, memory, fixture, voice, replay, and world-now operations. Many POST
routes are mutating. Daily reseed runs in live mode. Real voice start can
create and dial a Twilio call. The route blocks those production operations in
some paths, but the surface remains dangerous for future changes.

`services/api/routes/webhooks/twilio_simulator_proxy.py` validates Twilio
signatures, resolves a live assignment, creates a short-lived signed voice
route token, and forwards to the Worker. The Worker then requires the shared
proxy secret for `/proxy/*` routes.

### daily worker

`services/worker/handlers/system/simulator_daily_run.py:4314-4635` gates live
daily runs, inserts one run per organization and local date, reseeds data,
plans events, starts confirmations and callout workflows, waits for outcomes,
records counts, and publishes Slack and judge work.

`_prepare_daily_events` is deterministic. It plans callout and memory events
from the date and organization. `_inject_daily_event_memories` changes only
Worker memory. `_evaluate_callout_events` checks a product confirmation
decline. `_verify_memory_events` checks product-side extraction.

`services/worker/handlers/system/simulator_world_tick.py` is a separate
continuous world path. It records world events, may persist synthetic inbound
SMS, and can place daily voice calls. It has its own automatic cron gate.

### admin web and world display

`apps/web/routes/_app/admin/caregiver-sim.tsx` is the operator surface for
personas, conversations, scenarios, memory, details, voice, and reset.
`apps/web/routes/_app/admin/simulator-world.tsx` owns the replay and world
controls. `apps/web/routes/simulator-world/display.tsx` embeds the visual
world and polls the live display API.

`tools/sim_world/src/replay_director.ts` converts a persisted daily replay
into a dramatized movement and conversation schedule. It is presentation code,
not a workflow ledger.

`tools/sim_world/src/world_scene.ts` owns Phaser loading, input, camera state,
live polling, actor movement, labels, bubbles, and parent messaging. It is a
large scene object with several distinct responsibilities.

The display API has an authenticated `/simulator-world-display/live` route and
an unauthenticated `/simulator-world-display/public-live` route. The public
payload includes persona names, patient names, status, timing, and event
details. This is a deliberate public-display choice, but it should be reviewed
as a data exposure boundary.

## 3. defect and tech-debt catalog

### critical

1. **Lexical speech-language detection.**
   - Where: `tools/caregiver_sim/src/voice.ts:108-216`.
   - What: Spanish and English word sets, phrase regular expressions, token
     matching, and punctuation decide the TwiML language.
   - Why: This violates the project rule against phrase, regex, substring, or
     keyword matching for speech interpretation and voice behavior. It also
     fails on paraphrases, short bilingual turns, and supported languages.
   - Fix direction: use a provider or model language signal with a structured
     contract. Keep the runtime decision separate from free text.

2. **Lexical no-call preference detection.**
   - Where: `tools/caregiver_sim/src/memory.ts:269-289` and
     `personality_agent.ts:2852-2867`.
   - What: regular expressions inspect caregiver text and memory descriptions.
     The result suppresses or allows voice behavior.
   - Why: This violates the same hard rule. A structured memory event must own
     the preference and its confidence.
   - Fix direction: have the model or product tool emit an explicit preference
     event. Do not replace these patterns with a larger list.

### high

3. **Daily event SMS cannot target the production sender.**
   - Where: `services/worker/handlers/system/simulator_daily_run.py:2226-2239`.
   - What: event setup selects only an SMS-enabled `MOCK` phone.
   - Why: production has a Twilio sender. Memory events will have no system
     phone target, then fail as `memory_event_missing_target`. The event spec
     expects a mock bridge, while production uses real Twilio routing.
   - Fix direction: define one explicit event transport. For live production,
     resolve the approved simulator sender and assignment route. For mock mode,
     require the mock bridge. Test both paths.

4. **Voice template drift can create unintended escalation.**
   - Where: production template state; seed logic in
     `libraries/python/caregiver_sim_seed/outreach_templates.py:49-69`.
   - What: no active voice route exists, but the stored callout template still
     contains one-minute voice escalation.
   - Why: the sender is real Twilio and voice-enabled. A pending callout can
     reach telephony even though the seed currently says voice is disabled.
   - Fix direction: make sender, route, and template one versioned capability
     state. Add a read-only invariant report and refuse unsafe live execution.

5. **Production sender and assignment environment names are unclear.**
   - Where: production `organization_phone_numbers` and
     `simulator_phone_assignments`; constants in
     `libraries/python/caregiver_sim_seed/daily_simulator_phone_pool.py:11-24`.
   - What: assignments say `staging-daily`, but target the production API.
   - Why: operators can mistake a shared pool for a staging pool. Number swaps
     can route a live Twilio number to the wrong stack.
   - Fix direction: store explicit environment, org, webhook, and ownership
     metadata. Validate all assignments against the deployment environment.

6. **Direct Worker mutation and cost surface lacks app authentication.**
   - Where: `tools/caregiver_sim/src/index.ts:1025-1630` and
     `personality_agent.ts:414-457`.
   - What: `/simulate`, `/scenario/run`, memory writes, reflection, reset, and
     logs are exposed by the Worker router. The direct routes do not require
     the API admin dependency. `/proxy/*` has a shared secret, but the direct
     harness does not.
   - Why: the public Worker domain can become an unbounded LLM cost, data read,
     or Durable Object reset surface if its network boundary is misconfigured.
   - Fix direction: require a signed admin service token on every non-Twilio
     route. Keep direct Twilio routes limited to validated Twilio requests.

7. **Unauthenticated world payload includes names and patient details.**
   - Where: `caregiver_sim.py:5917-5924` and
     `caregiver_sim.py:5490-5784`.
   - What: `/simulator-world-display/public-live` returns persona names,
     patient names, statuses, times, labels, and event details without a token.
   - Why: a public display endpoint can expose more simulated agency data than
     the display needs. The endpoint is also easy to copy outside the intended
     screen.
   - Fix direction: return opaque display labels and coarse status in public
     mode. Keep detailed payloads behind the bearer token.

8. **Two competing outreach filter implementations.**
   - Where: `libraries/python/outreach_filtering/filters.py:86-146` and
     `libraries/python/outreach_operations/exclusions.py:35-123`.
   - What: the new callout evaluator handles conditional client exceptions,
     while the generic filter has no exception semantics.
   - Why: a caller can report 30 excluded caregivers while a different caller
     silently filters all 32 or treats a conditional row as unconditional.
   - Fix direction: expose one evaluator with explicit shift context. Delete or
     quarantine the old path after consumer migration.

### medium

9. **Worker schema migration is implicit.**
   - Where: `personality_agent.ts:285-412`.
   - What: Durable Object startup runs `CREATE TABLE IF NOT EXISTS` and catches
     a string match for a duplicate column.
   - Why: schema versions are not recorded. A future migration cannot prove
     which objects have which schema. Error matching is fragile.
   - Fix direction: add a schema version table and numbered migrations.

10. **Simulator identity is duplicated across Python and TypeScript.**
    - Where: `caregiver_sim_seed/personas.py` and
      `tools/caregiver_sim/src/personalities.ts`.
    - What: phone maps, names, roles, and daily fallback behavior exist in two
      languages.
    - Why: a seed can point at one persona while the Worker resolves another.
      The duplicated production/development maps already reuse real numbers in
      development defaults.
    - Fix direction: publish a versioned persona manifest. Validate it at seed,
      deploy, and proxy resolution time.

11. **Seed updates are not clearly ownership-scoped.**
    - Where: `caregiver_details.py`, `caregiver_records.py`,
      `shift_records.py`, and `seed_data.py`.
    - What: seed helpers update availability, preferences, memory, caregiver
      fields, and marked shifts. Generic old rows and some old memory fragments
      are not fully pruned.
    - Why: reseed can overwrite operator changes or leave stale state that
      affects matching and replay.
    - Fix direction: add seed ownership columns or namespaces. Reconcile only
      seed-owned rows. Report orphaned rows without changing them.

12. **Worker memory is unbounded and weakly linked.**
    - Where: `personality_agent.ts:2672-2825`.
    - What: reflections append new thought rows. Evidence IDs are JSON text and
      message deletion uses a `LIKE` search. Global events apply to every
      thread.
    - Why: repeated reflection grows state. Text matching can delete the wrong
      event. Global memory can cross-contaminate threads.
    - Fix direction: normalize evidence links, add dedupe keys and retention,
      and make global memory scope explicit.

13. **Scenario harness bypasses real workflow records.**
    - Where: Worker `/scenario/run` and API route
      `caregiver_sim.py:1073-1153`; the unfinished items in
      `experimental/phoebe_simulator_execplan.md`.
    - What: controller and caregiver models converse directly. The harness
      records Worker scenario rows but does not always run Phoebe outbound and
      inbound workflow paths.
    - Why: a green scenario can miss matching, outbox, extraction, status, or
      routing defects.
    - Fix direction: keep the harness for fast unit scenarios, but add a
      first-class workflow-run ledger that points to real product records.

14. **Daily automatic execution has weak operational evidence.**
    - Where: daily and world tick handlers; production run data.
    - What: daily runs stop at September 2, world events are empty, and recent
      outreach activity is partial.
    - Why: operators cannot tell whether the scheduler is disabled, the queue
      is not dispatching, or the handler is skipping on configuration.
    - Fix direction: publish a heartbeat with gate state, last attempted run,
      last successful run, sender mode, assignment count, and skip reason.

15. **The world scene is a large presentation monolith.**
    - Where: `tools/sim_world/src/world_scene.ts`.
    - What: one Phaser scene owns API polling, input, camera, live actor state,
      replay state, movement, bubbles, and parent messaging.
    - Why: visual changes can alter network or replay behavior. Tests need a
      full Phaser environment for small state changes.
    - Fix direction: split transport, world model, replay clock, actor view,
      and camera controller. Keep Phaser at the edge.

16. **Replay and live time are not fully reproducible.**
    - Where: `replay_director.ts`, `world_scene.ts`, and the daily Worker.
    - What: replay uses a synthetic clock, while live actors use `Math.random()`
      for placement and idle movement. Worker reply delays also use random
      values.
    - Why: an operator cannot reproduce the same visual or conversational
      sequence from one run record.
    - Fix direction: pass a run seed through every random source and persist
      the selected seed with the run.

17. **Cross-conversation summaries can leak context.**
    - Where: `personality_agent.ts:460-527` and its summary queries.
    - What: a persistent thread receives summaries from other counterparty
      threads.
    - Why: one caregiver conversation can influence another without an explicit
      scope decision. This also makes scenario results harder to explain.
    - Fix direction: make cross-thread memory opt-in and show the exact source
      IDs in the inspector.

18. **Parent messaging uses a wildcard target in the world scene.**
    - Where: `tools/sim_world/src/world_scene.ts:729-734`.
    - What: the iframe sends parent events with `postMessage(message, '*')`.
    - Why: the admin parent validates inbound origin, but wildcard output is
      broader than needed and weakens the embed boundary.
    - Fix direction: configure and use the known parent origin.

## 4. prioritized refactor plan

1. **Make telephony capability explicit and safe.** Scope: medium.
   Create one live capability record for sender mode, assignment pool,
   webhook target, voice route, and template mode. Add a read-only invariant
   endpoint. Block unsafe daily execution when the state is inconsistent.

2. **Remove forbidden lexical voice decisions.** Scope: medium.
   Replace language and no-call decisions with structured provider/model output.
   Add provider-backed evaluations for paraphrases, partial turns, supported
   languages, and live-human false positives. Do not add another phrase list.

3. **Unify conditional outreach evaluation.** Scope: small to medium.
   Migrate all callout and recommendation consumers to one shift-aware
   evaluator. Add tests for Galiver, non-Galiver, missing-client, stale-rule,
   and concurrent-rule cases.

4. **Repair the daily event transport contract.** Scope: medium.
   Separate mock and live event transports. Resolve the production simulator
   sender through the same approved assignment and proxy path as normal
   outreach. Record transport, target assignment, and delivery evidence in the
   run row.

5. **Add scheduler and configuration heartbeat evidence.** Scope: small.
   Record each daily and world tick gate result. Include automatic setting,
   environment, organization resolution, sender mode, and last queue action.
   Alert when the last successful run exceeds one local day.

6. **Harden Worker boundaries.** Scope: medium.
   Require service authentication for direct admin and diagnostic routes.
   Separate Twilio webhook authentication from admin authentication. Restrict
   proxy target hosts to approved deployment URLs. Add request size, turn, and
   cost limits at the Worker boundary.

7. **Create a versioned persona manifest.** Scope: medium.
   Generate Python and TypeScript views from one manifest. Include phone
   ownership, environment, language capability, route target, and seed version.
   Fail closed on duplicate or environment-incompatible assignments.

8. **Define seed ownership and reconciliation.** Scope: medium to large.
   Mark rows and memory fragments as seed-owned. Reconcile only those rows.
   Report stale shifts, clients, availability, and memories. Add a dry-run
   diff before any live reseed.

9. **Build a workflow-run ledger.** Scope: large.
   Keep the fast Worker scenario harness, but link it to a product workflow
   run. Store inputs, model selections, retrieved memory IDs, outbox IDs,
   confirmations, outreach IDs, and terminal state in one inspectable record.

10. **Split the visual world layer.** Scope: medium to large.
    Extract API transport, normalized world state, replay clock, actor model,
    and Phaser rendering. Use a deterministic run seed. Keep the current map
    as a view adapter, not as the state model.

11. **Add Durable Object migrations and memory retention.** Scope: medium.
    Add numbered schema migrations, normalized evidence links, reflection
    dedupe, per-thread scope, and bounded retention. Expose migration health in
    the admin diagnostics page.

## 5. open questions for Henry

1. Should production daily runs use real Twilio for all simulated caregiver
   traffic, or should daily event validation remain mock-only?
2. If real Twilio is the policy, which sender and assignment environment name
   is canonical: production, staging, or a separately named daily pool?
3. Should missing voice routing disable the callout voice step immediately, or
   should the organization gain an explicit simulator-only voice destination?
4. Are the two manual/demo caregivers meant to remain in the daily org? If yes,
   should their Galiver exception be a named policy instead of a tag inference?
5. Should the 52 inactive Galiver clients and past open shifts be archived,
   or are they needed for historical replay?
6. Is the public world display intended to expose patient names and event
   details without authentication? If not, what is the minimum public payload?
7. Should direct Worker routes be private behind the API, or is a separate
   signed operator link required for local and production diagnostics?
8. Which product workflow must own automatic refill after an SMS decline? The
   current daily event spec still leaves this as an open product decision.
9. Should cross-conversation memory be allowed between counterparties for one
   persona, or must every memory be thread-scoped?
10. Is the pixel map still a required demo surface? If not, should the refactor
    prioritize the operational event ledger and inspector first?

## Evidence and review limits

The audit used aggregate production queries only. It did not read or include
raw message bodies, phone numbers, or customer contact data. No tests were
run because the contract prohibited local services and Bazel. No production
mutation was attempted.
