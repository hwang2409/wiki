---
type: reference
tags: [phoebe, agent, write-tools, audit]
created: 2026-08-18
updated: 2026-08-18
---

# customer-facing agent write-tools epic audit

## contents

- [scope and method](#scope-and-method)
- [executive summary](#executive-summary)
- [voice](#voice)
- [sms](#sms)
- [admin crud](#admin-crud)
- [workflow and temporal](#workflow-and-temporal)
- [feature flags and settings](#feature-flags-and-settings)
- [other side effects](#other-side-effects)
- [reconciliation appendix](#reconciliation-appendix)
- [manual verification queue](#manual-verification-queue)

## scope and method

This audit covers customer-facing SMS, voice, and in-app coordinator chat.
It excludes `phoebe_admin_agent*` and `phoebe_v3_agent`.

The two source audits were:

1. `vault/phoebe/agent-write-tools-audit-2026-08-18.md`.
2. `/Users/henry/downloads/agent_v3_write_tools.md`.

I enumerated current registrations from `origin/main`. I traced direct ORM
writes, SQLAlchemy statements, queue publishes, Redis state, Temporal signals,
and provider calls. `app.*` is the PostgreSQL schema unless noted otherwise.

The requested `libraries/python/llm_messaging_engine` path does not exist in
current `origin/main`. The current SMS engine is
`libraries/python/sms_conversation_engine`.

`30d usage` uses the first source audit's 2026-07-19 through 2026-08-18
window. A zero means the source audit reported zero, not that code is dead.

## executive summary

| surface | audited registered write tools | high | medium | low |
|---|---:|---:|---:|---:|
| sms | 17 | 7 | 8 | 2 |
| voice | 14 | 8 | 5 | 1 |
| in-app customer-facing chat | 59 | 19 | 31 | 9 |
| total | 90 | 34 | 44 | 12 |

The count treats one logical tool name as one tool. Duplicate registry mounts
are listed in the affected entry. Voice clock tools are not counted because
current agents exclude them; post-call actions appear in a separate note.

Top five highest-risk tools:

1. `manage_outreach_start_with_contacts` — starts live caregiver contact.
2. `manage_outreach_add_contact_attempts` — adds live contact attempts.
3. `fill_callout_shifts` — assigns shifts and can write back to an EHR.
4. `cancel_callout_shifts` — cancels shifts, notifications, and workflows.
5. `send_one_off_sms_in_conversation` — sends an immediate caregiver SMS.

The main control gap is split ownership. Many LLM tools only enqueue a
payload. A worker then performs the business write. Tool-level audits that
count only `session.add` miss that downstream effect.

## entry template

Each entry records the registration line, purpose, surface, gate, payload,
confirmation, direct DB footprint, mode requirement, external effects, usage,
risk, and known issues. `C`, `U`, and `D` mean create, update, and delete.
“Downstream” means the tool itself publishes or signals; the listed worker
performs the later write.

## sms

### `set_caregiver_availability`

- registry: `libraries/python/sms_conversation_engine/tools/availability_tools.py:239`
- purpose/surface: replace one caregiver's dated availability; SMS customer agent.
- gate: registered by `build_engine_tool_group`; organization and caregiver context are required. No Henry-only gate. Clock and cancellation gates do not control this tool.
- payload: required `start_time`, `end_time`; optional none.
- confirmation: single-shot; the SMS conversation asks for the interval before calling.
- db: `app.org_caregiver_availability` C/U/D; helper replaces overlapping rows. Typical invocation deletes overlapping rows and creates one row. Exact row count depends on overlap.
- mode_context: yes through organization-scoped session helpers; `organization_id` and caregiver context are required.
- external: publishes `CaregiverAvailabilityConflictNotifyPayload` when a scheduled-shift conflict needs follow-up.
- 30d usage: 2,178.
- risk: high — changes caregiver availability and can affect future matching.
- bugs/tickets: source audit noted no open ticket; needs manual verification of helper row counts.

### `set_caregiver_unavailability`

- registry: `libraries/python/sms_conversation_engine/tools/availability_tools.py:359`
- purpose/surface: replace a dated unavailable interval; SMS customer agent.
- gate: organization and caregiver context; unrestricted to an authenticated customer conversation.
- payload: required `start_time`, `end_time`; optional `reason` (`time_off_request` or `shift_offer_decline`).
- confirmation: single-shot after the caregiver states the interval.
- db: `app.org_caregiver_availability` C/U/D; same overlap-replacement helper as availability.
- mode_context: yes; organization-scoped.
- external: conflict notification queue message when needed.
- 30d usage: 17,410.
- risk: high — can remove a caregiver from matching.
- bugs/tickets: needs manual verification of overlap deletion behavior.

### `set_caregiver_weekly_availability`

- registry: `libraries/python/sms_conversation_engine/tools/availability_tools.py:704`
- purpose/surface: replace recurring weekly availability blocks; SMS customer agent.
- gate: organization and caregiver context; no Henry-only gate.
- payload: required `day_of_week`, `start_time`, `end_time`; optional none.
- confirmation: single-shot.
- db: `app.org_caregiver_weekly_availability` C/U/D; helper splits overnight ranges and replaces overlapping fragments. One call can create multiple rows.
- mode_context: yes; organization-scoped.
- external: none on the normal path.
- 30d usage: 17,910.
- risk: high — recurring availability changes affect future staffing.
- bugs/tickets: needs manual verification of split-row count for overnight ranges.

### `set_caregiver_weekly_unavailability`

- registry: `libraries/python/sms_conversation_engine/tools/availability_tools.py:912`
- purpose/surface: replace recurring weekly unavailability blocks; SMS customer agent.
- gate: organization and caregiver context; no Henry-only gate.
- payload: required `day_of_week`, `start_time`, `end_time`; optional none.
- confirmation: single-shot.
- db: `app.org_caregiver_weekly_availability` C/U/D; overlap fragments are removed and unavailable fragments are created.
- mode_context: yes; organization-scoped.
- external: none.
- 30d usage: 4,330.
- risk: high — recurring staffing eligibility changes.
- bugs/tickets: needs manual verification of retained-fragment behavior.

### `record_caregiver_preference_answer`

- registry: `libraries/python/sms_conversation_engine/tools/caregiver_preference_tools.py:65`
- purpose/surface: advance the caregiver preference survey; SMS customer agent.
- gate: active survey session and caregiver context; no Henry-only gate.
- payload: required `action`, `question_key`; optional survey-specific values including hours, area, travel range, employment, languages, days, and service preferences.
- confirmation: multi-step survey state machine.
- db: `app.caregiver_preference_survey_sessions` U; one session transition per invocation. The tool's direct SQL loads and advances the session through `advance_survey_session`.
- mode_context: needs manual verification; the current helper receives organization context but the transition API hides its mode handling.
- external: none in the tool; response text is returned to the SMS engine.
- 30d usage: 1,931.
- risk: medium — changes a caregiver's matching preferences.
- bugs/tickets: source audit noted survey routing and language behavior; verify helper table writes.

### `handle_clock_in_tool`

- registry: `libraries/python/sms_conversation_engine/tools/clock_tools.py:223`
- purpose/surface: request a clock-in writeback for a shift; SMS customer agent.
- gate: `clock_in_writes_enabled`; shift and organization context required.
- payload: required `shift_id`; optional `clock_hour`, `clock_minute`, `clock_date`, `timezone`.
- confirmation: single-shot after the caregiver supplies a time.
- db: direct read only; downstream `ShiftClockInExecutePayloadV1` writes `app.shifts` and `app.shift_progressions` through the clock worker. One shift write and one progression row are expected downstream.
- mode_context: yes in the payload and worker; organization-scoped.
- external: queue publish.
- 30d usage: 2,305.
- risk: high — changes legal timekeeping data.
- bugs/tickets: source audit identified the org feature gate; verify worker write count.

### `handle_clock_out_tool`

- registry: `libraries/python/sms_conversation_engine/tools/clock_tools.py:355`
- purpose/surface: request a clock-out writeback; SMS customer agent.
- gate: `clock_out_writes_enabled`; shift and organization context required.
- payload: required `shift_id`; optional `clock_hour`, `clock_minute`, `clock_date`, `timezone`.
- confirmation: single-shot.
- db: direct read only; downstream `ShiftClockOutExecutePayloadV1` updates `app.shifts` and creates `app.shift_progressions` when accepted.
- mode_context: yes in payload and worker.
- external: queue publish.
- 30d usage: 2,333.
- risk: high — changes legal timekeeping data.
- bugs/tickets: verify downstream write count and EHR writeback status.

### `handle_clock_in_reminder_response_tool`

- registry: `libraries/python/sms_conversation_engine/tools/clock_tools.py:650`
- purpose/surface: record a response to a clock-in reminder; SMS customer agent.
- gate: active reminder context; no separate Henry-only gate.
- payload: optional `expected_minutes_until_clock`.
- confirmation: single-shot response.
- db: `app.contact_attempts` U; one reminder response timestamp/outcome update per invocation. Helper may also update related attempt state.
- mode_context: yes through organization-scoped context.
- external: none at tool level.
- 30d usage: 2,314.
- risk: medium — changes reminder audit state but not the clock itself.
- bugs/tickets: source audit says post-call code also updates reminder bookkeeping; verify duplicate-write behavior.

### `handle_clock_out_reminder_response_tool`

- registry: `libraries/python/sms_conversation_engine/tools/clock_tools.py:667`
- purpose/surface: record a response to a clock-out reminder; SMS customer agent.
- gate: active reminder context.
- payload: optional `expected_minutes_until_clock`.
- confirmation: single-shot response.
- db: `app.contact_attempts` U; one reminder response/outcome update per invocation.
- mode_context: yes; organization-scoped.
- external: none at tool level.
- 30d usage: 2,034.
- risk: medium — changes reminder audit state.
- bugs/tickets: verify duplicate post-call bookkeeping.

### `send_contact_card_tool`

- registry: `libraries/python/sms_conversation_engine/tools/contact_card_tools.py:54`
- purpose/surface: send the caregiver a contact card link; SMS customer agent.
- gate: caregiver and organization context; no confirmation prompt.
- payload: no caller payload beyond context.
- confirmation: single-shot.
- db: `app.organization_onboarding_settings` C/U; upsert creates or updates one contact-card slug row. Related caregiver/phone rows are read.
- mode_context: yes; organization-scoped.
- external: Twilio/SMS provider through `send_sms_message_idempotent`.
- 30d usage: 271.
- risk: medium — sends an external message and persists a public-link slug.
- bugs/tickets: verify idempotency key and slug exposure.

### `mark_conversation_complete_tool`

- registry: `libraries/python/sms_conversation_engine/tools/conversation_tools.py:16`
- purpose/surface: stop the current SMS conversation; SMS customer agent.
- gate: conversation context; unrestricted once mounted.
- payload: optional `reason`.
- confirmation: single-shot.
- db: no direct DB write found in the tool. It returns a completion control consumed by the engine.
- mode_context: no.
- external: stops further engine replies; no provider call in this function.
- 30d usage: 14,367.
- risk: low — changes conversation control, not a business row.
- bugs/tickets: source audit classifies this as a side effect; verify engine persistence outside this function.

### `record_intent_tool`

- registry: `libraries/python/sms_conversation_engine/tools/intent_tools.py:22`
- purpose/surface: record the classified SMS intent; SMS customer agent.
- gate: tool group mount only.
- payload: required `intent` from the supported enum.
- confirmation: single-shot.
- db: no app table write found; observability metric/event only.
- mode_context: no.
- external: metrics/logging side effect.
- 30d usage: 6,978.
- risk: low — no business-state mutation found.
- bugs/tickets: keep in the inventory because the source audit counts persisted observability side effects.

### `save_memory`

- registry: `libraries/python/sms_conversation_engine/tools/memory_tools.py:48`
- purpose/surface: save a durable caregiver memory from SMS; SMS customer agent.
- gate: caregiver context; no Henry-only gate.
- payload: required `note`.
- confirmation: single-shot, after the caregiver states the fact.
- db: `app.org_caregiver_memories` C/U and `app.org_caregiver_memory_fragments` C; one memory upsert and one fragment per new fact, or one fragment on an existing memory.
- mode_context: yes; organization-scoped.
- external: publishes `MemoryCompilePayload` after commit.
- 30d usage: 6,895.
- risk: medium — durable personal data persists and enters memory compilation.
- bugs/tickets: verify redaction and compile retry behavior.

### `handle_callout_response_tool_v2`

- registry: `libraries/python/sms_conversation_engine/tools/outreach_response_tools.py:28`
- purpose/surface: record caregiver responses to offered shifts; SMS customer agent.
- gate: live contact-attempt context; organization and attempt IDs are validated.
- payload: required `contact_attempt_id`, `shift_responses`; optional `needs_review_owner`.
- confirmation: single-shot after the caregiver answers.
- db: no direct DB write; downstream `CalloutsResponseHandleV2Payload` updates `app.contact_attempts`, `app.contact_attempt_shift_responses`, `app.outreaches`, and related shift state. Rows vary with offered shifts.
- mode_context: yes in the payload and downstream worker.
- external: queue publish.
- 30d usage: 53,004.
- risk: high — accepts or declines live work and can trigger outreach actions.
- bugs/tickets: source audit flags downstream response handling; verify exact worker row set.

### `handle_upcoming_shift_cancellation_tool`

- registry: `libraries/python/sms_conversation_engine/tools/shift_cancellation_tools.py:279`
- purpose/surface: submit an upcoming-shift cancellation; SMS customer agent.
- gate: cancellation feature flag and a valid caregiver shift context. Open-shift cancellation can require a prior clarification.
- payload: required `shift_id`.
- confirmation: multi-step; `prepare_shift_cancellation_selection_tool` is control-only and not counted as a write tool.
- db: direct `app.shifts` U; one cancellation update per invocation. Helper may write cancellation audit/event rows outside this function.
- mode_context: yes; organization-scoped.
- external: cancellation workflow/notification side effects downstream.
- 30d usage: 211.
- risk: high — removes an upcoming caregiver commitment.
- bugs/tickets: source audit notes reliability-warning behavior; verify cancellation audit rows.

### `handle_shift_confirmation_response_tool`

- registry: `libraries/python/sms_conversation_engine/tools/shift_confirmation_response_tools.py:26`
- purpose/surface: accept or decline a shift confirmation; SMS customer agent.
- gate: valid `shift_confirmation_id` from the conversation.
- payload: required `shift_confirmation_id`, `response`.
- confirmation: single-shot.
- db: no direct DB write; downstream `ShiftConfirmationsResponseHandlePayload` updates `app.shift_confirmations`, `app.shifts`, and related response/audit rows. Usually one confirmation and one shift state transition.
- mode_context: yes in payload and downstream handler.
- external: queue publish.
- 30d usage: 71,398.
- risk: high — commits or rejects a staffed shift.
- bugs/tickets: verify downstream row count for decline versus accept.

### `offer_open_shifts_tool`

- registry: `libraries/python/sms_conversation_engine/tools/shift_discovery_tools.py:156`
- purpose/surface: offer open shifts to a caregiver; SMS customer agent.
- gate: shift-discovery feature gate; current assembly may exclude the group.
- payload: optional `window_start_date`, `window_end_date`.
- confirmation: multi-step discovery flow.
- db: helper can create or reuse an `app.outreaches` row and related `app.outreach_shifts`/response rows; exact path depends on reusable-offer state.
- mode_context: yes; organization-scoped.
- external: queue and SMS offer side effects.
- 30d usage: 0.
- risk: high — can create a new caregiver outreach.
- bugs/tickets: source audit reports zero use; verify current runtime mount and helper row set.

## voice

Current voice agents always add `end_call`, `transfer_call`, and
`mark_as_voicemail`. Scenario tools mount only on matching agent types. The
clock tool registrations remain in source but current clock agents return an
empty scenario group; post-call QA owns those writes.

### `end_call`

- registry: `services/voice/tools/core.py:549`
- purpose/surface: end a live call; voice customer agent.
- gate: caller-floor and end-call safety checks; no Henry-only gate.
- payload: context only; optional farewell text is internal context.
- confirmation: single-shot after the model decides the call is complete.
- db: no direct DB write found; call completion is persisted by the call/session worker outside the tool.
- mode_context: no direct org DB context.
- external: Twilio call hangup, Redis end-call state, and terminal completion signals.
- 30d usage: 330,874.
- risk: high — terminates a live external call.
- bugs/tickets: source audit reports call-end fallback paths; verify final call record persistence.

### `transfer_call`

- registry: `services/voice/tools/core.py:727`
- purpose/surface: transfer the caller to a human destination; voice customer agent.
- gate: destination and transfer safety checks; context must permit transfer.
- payload: context only; optional destination is resolved from variables.
- confirmation: single-shot; transfer guard prevents duplicate claims.
- db: no direct DB write found.
- mode_context: no direct DB write.
- external: Twilio transfer API and Redis transfer-claim keys.
- 30d usage: 40,808.
- risk: high — routes a live call to an external destination.
- bugs/tickets: source audit flags transfer retries and muted-line cases; verify provider failure cleanup.

### `mark_as_voicemail`

- registry: `services/voice/tools/core.py:1166`
- purpose/surface: classify the call as voicemail; voice customer agent.
- gate: voicemail handler and call context.
- payload: context only.
- confirmation: single-shot.
- db: no direct DB write found; downstream call outcome worker updates the call attempt record.
- mode_context: no direct DB write.
- external: Redis voicemail classification and call termination.
- 30d usage: 197,705.
- risk: high — changes call disposition and can suppress follow-up.
- bugs/tickets: source audit lists voicemail outcome RCA work; verify downstream disposition table.

### `handle_callout_response`

- registry: `services/voice/tools/outreach.py:113`
- purpose/surface: record an outbound caregiver response; voice customer agent.
- gate: mounted on outbound outreach agents; excluded when required context is missing.
- payload: context contains contact attempt and shift responses; schema is encoded in `CalloutsResponseHandleV2Payload`.
- confirmation: single-shot.
- db: no direct DB write; downstream updates `app.contact_attempts`, `app.contact_attempt_shift_responses`, and outreach/shift state.
- mode_context: yes in payload/downstream.
- external: Redis dedupe state and queue publish.
- 30d usage: 22,999.
- risk: high — changes live outreach outcomes.
- bugs/tickets: verify deferred-response flush and duplicate suppression.

### `handle_outreach_response`

- registry: `services/voice/tools/inbound_outreach.py:42`
- purpose/surface: record an inbound caregiver outreach response; voice customer agent.
- gate: mounted on inbound pending-response agent; response claim state is required.
- payload: context only; pending response is resolved from call variables.
- confirmation: single-shot with Redis claim/replace protection.
- db: no direct DB write; downstream `CalloutsResponseHandleV2Payload` updates contact-attempt and shift-response rows.
- mode_context: yes in payload/downstream.
- external: Redis state and queue publish.
- 30d usage: 1,559.
- risk: high — commits an inbound work response.
- bugs/tickets: verify claim expiry and queue retry behavior.

### `set_caregiver_availability` / `set_caregiver_unavailability`

- registry: `services/voice/tools/inbound_availability.py:429`, `:447`
- purpose/surface: set dated caregiver availability or unavailability; inbound voice customer agent.
- gate: inbound pending-response agent, valid caregiver context, and organization mode. No direct DB write is performed here.
- payload: context carries interval, reason, caregiver, and organization.
- confirmation: single-shot after spoken interval confirmation.
- db: no direct DB write; downstream `CaregiverAvailabilitySetPayload` updates `app.org_caregiver_availability` with overlap replacement. One or more rows vary by overlap.
- mode_context: required and explicitly validated.
- external: queue publish; conflict path may transfer the call.
- 30d usage: availability 232; unavailability 149.
- risk: high — changes caregiver matching eligibility.
- bugs/tickets: verify voice payload mode and conflict transfer path.

### `mark_declined_shifts_unavailable`

- registry: `services/voice/tools/inbound_availability.py:484`
- purpose/surface: mark shifts declined during outreach as unavailable; outbound voice customer agent.
- gate: outbound outreach group only; exact shift list comes from call context.
- payload: context only.
- confirmation: single-shot.
- db: no direct DB write; downstream availability handler updates `app.org_caregiver_availability` and related shift response state.
- mode_context: yes in payload/downstream.
- external: queue publish and possible conflict handling.
- 30d usage: 5,634.
- risk: high — changes availability after a decline.
- bugs/tickets: verify whether each shift creates a separate availability row.

### `handle_shift_confirmation_response` / `handle_upcoming_shift_cancellation`

- registry: `services/voice/tools/inbound_shift_confirmation.py:44`, `:220`
- purpose/surface: accept/decline a confirmation or request a cancellation; inbound voice customer agent.
- gate: inbound pending-response agent; cancellation also requires the voice cancellation feature flag.
- payload: context only; pending IDs resolve from call variables.
- confirmation: single-shot response; cancellation uses transfer thresholds and safety checks.
- db: no direct DB write; queue handlers update `app.shift_confirmations`, `app.shifts`, response/audit rows, and may cancel outreach links.
- mode_context: yes in payload/downstream.
- external: Redis idempotency keys, queue publish, and transfer on unsafe/near-term cancellation.
- 30d usage: confirmation 524; cancellation 176.
- risk: high — changes shift commitment or cancels work.
- bugs/tickets: verify threshold behavior and duplicate response claims.

### `record_caregiver_preference_answer_voice`

- registry: `services/voice/tools/caregiver_preference_survey.py:115`
- purpose/surface: advance the caregiver preference survey by voice; voice customer agent.
- gate: active survey session and voice transition API authorization.
- payload: required `action`, `question_key`; optional question-specific arrays, strings, booleans, and integers.
- confirmation: multi-step survey.
- db: no direct DB write; transition API writes `app.caregiver_preference_survey_sessions` and related caregiver preference records. Exact API row set needs manual verification.
- mode_context: needs manual verification; API request carries voice session identity, not a visible SQLAlchemy mode guard.
- external: internal HTTP transition API.
- 30d usage: 899.
- risk: medium — changes durable matching preferences.
- bugs/tickets: verify API authorization and mode propagation.

### `record_shift_discovery_offer` / `record_shift_discovery_responses` / `express_shift_interest`

- registry: `services/voice/tools/shift_discovery.py:124`, `:386`, `:462`
- purpose/surface: record an open-shift offer, caregiver responses, or interest; voice customer agent.
- gate: `shift_discovery_enabled`; outbound and inbound agents exclude the group when false.
- payload: context; response tool includes matched shift responses from the call.
- confirmation: multi-step offer and response flow.
- db: no direct DB write; queue handlers write `app.outreaches`, `app.outreach_shifts`, `app.contact_attempt_shift_responses`, and related shift-interest rows. Exact rows vary by matched shift count.
- mode_context: yes in payload/downstream.
- external: Redis offer persistence and queue publish.
- 30d usage: all 0.
- risk: medium — creates or changes open-shift outreach state.
- bugs/tickets: source audit reports zero use; verify runtime registration before enabling.

## admin crud

These are coordinator-chat CRUD tools. “admin CRUD” here means customer
organization records, outreach records, shifts, and caregiver/client data. It
does not mean the excluded internal admin agent.

### `save_note`

- registry: `libraries/python/phoebe_event_agent/tools/agent_notes.py:56`
- purpose/surface: save a coordinator note for a caregiver, client, or shift; in-app chat.
- gate: agent-note rollout and domain routing; user must have the organization context. Approval cards apply unless an always-allow rule exists.
- payload: required `content`; optional `caregiver_id`, `client_id`, `shift_id`.
- confirmation: single-shot, usually shown as an approval card.
- db: `app.agent_notes` C/U; one deduplicated note row per invocation.
- mode_context: yes.
- external: activity/event emission.
- 30d usage: source audit did not report a separate count; needs manual verification.
- risk: medium — durable coordinator record.
- bugs/tickets: source audit predates the agent-note rollout; verify runtime exposure.

### `save_automation_memory` / `update_automation_memory` / `discard_automation_memory`

- registry: `libraries/python/phoebe_event_agent/tools/automations.py:223`, `:311`, `:408`
- purpose/surface: create, edit, or discard automation memory; in-app chat.
- gate: automation run scope and organization authorization; approval policy applies.
- payload: create requires `kind`, `content`, `reason`; update/discard require `memory_id`, `memory_version`, `reason`; update also requires `content`.
- confirmation: single-shot with version checks; no multi-step preview in the tool.
- db: `app.agent_automation_memories` C/U/D and `app.agent_automation_memory_events` C; one memory mutation and one audit event per invocation.
- mode_context: yes; automation run scope is required.
- external: PostHog/analytics capture and automation event dispatch.
- 30d usage: source audit reported one automation-memory write across the set.
- risk: medium — changes durable automation behavior.
- bugs/tickets: verify which of the three tools received the one reported use.

### `add_caregiver_weekly_availability`

- registry: `libraries/python/phoebe_event_agent/tools/caregiver_availability.py:62`
- purpose/surface: add a recurring caregiver availability block; in-app chat.
- gate: organization context and caregiver validation; approval policy applies.
- payload: required `caregiver_id`, `status`, `start_minute_of_week`, `end_minute_of_week`.
- confirmation: single-shot approval.
- db: `app.org_caregiver_weekly_availability` C; one row per non-split interval.
- mode_context: yes.
- external: activity event.
- 30d usage: 98.
- risk: high — changes recurring staffing eligibility.
- bugs/tickets: verify whether helper splits overnight ranges.

### `add_caregiver_one_off_availability`

- registry: `libraries/python/phoebe_event_agent/tools/caregiver_availability.py:186`
- purpose/surface: add a dated caregiver availability block; in-app chat.
- gate: organization context, caregiver validation, and approval policy.
- payload: required `caregiver_id`, `status`, `start_time`, `end_time`.
- confirmation: single-shot approval.
- db: `app.org_caregiver_availability` C; one row per interval.
- mode_context: yes.
- external: activity event.
- 30d usage: 89.
- risk: high — changes matching eligibility.
- bugs/tickets: needs manual verification of overlap behavior.

### `delete_caregiver_availability`

- registry: `libraries/python/phoebe_event_agent/tools/caregiver_availability.py:288`
- purpose/surface: remove a dated or recurring availability row; in-app chat.
- gate: caregiver row ownership and approval policy.
- payload: required `availability_id`, `recurrence`.
- confirmation: single-shot approval.
- db: `app.org_caregiver_availability` or `app.org_caregiver_weekly_availability` D; one selected row or recurrence set.
- mode_context: yes.
- external: activity event.
- 30d usage: 7.
- risk: high — removes staffing availability.
- bugs/tickets: verify recurrence selector cannot delete the wrong row.

### `save_caregiver_memory_instruction`

- registry: `libraries/python/phoebe_event_agent/tools/caregiver_memory.py:67`
- purpose/surface: save a durable caregiver memory instruction; in-app chat.
- gate: caregiver must belong to the organization; memory routing and approval policy apply.
- payload: required `caregiver_id`, `instruction`.
- confirmation: single-shot approval.
- db: `app.org_caregiver_memories` C/U and `app.org_caregiver_memory_fragments` C; one memory upsert and one fragment.
- mode_context: yes.
- external: `MemoryCompilePayload`, memory events, and analytics.
- 30d usage: 1,218.
- risk: medium — persists personal data used by future agent runs.
- bugs/tickets: verify redaction and source attribution.

### `update_caregiver_record`

- registry: `libraries/python/phoebe_event_agent/tools/caregiver_record.py:55`
- purpose/surface: edit caregiver profile fields; in-app chat.
- gate: organization caregiver ownership; approval policy applies.
- payload: required `caregiver_id`; optional `agent_instructions`, `primary_language`, `supported_languages`, `roles`, `employment_type`, `desired_weekly_hours`.
- confirmation: single-shot approval.
- db: `app.org_caregiver_records` U; one row per invocation.
- mode_context: yes.
- external: activity event and profile update instrumentation.
- 30d usage: 4.
- risk: high — changes profile and matching data.
- bugs/tickets: source audit noted low use; verify field allowlist.

### `save_client_memory_instruction`

- registry: `libraries/python/phoebe_event_agent/tools/client_memory.py:67`
- purpose/surface: save a durable client memory instruction; in-app chat.
- gate: client organization ownership and memory routing.
- payload: required `client_id`, `instruction`.
- confirmation: single-shot approval.
- db: `app.org_client_memories` C/U and `app.org_client_memory_fragments` C; one memory upsert and one fragment.
- mode_context: yes.
- external: `MemoryCompilePayload`, memory events, analytics.
- 30d usage: 539.
- risk: medium — persists client data used by future runs.
- bugs/tickets: verify redaction and client authorization.

### `update_client_record`

- registry: `libraries/python/phoebe_event_agent/tools/client_record.py:82`
- purpose/surface: update client profile and care-plan fields; in-app chat.
- gate: client must belong to the organization; approval policy applies.
- payload: required `client_id`; optional `default_notify_on_fill`, `care_plan_summary` object with `summary` and `bullets`.
- confirmation: single-shot approval.
- db: `app.org_client_records` U; one row.
- mode_context: yes.
- external: activity event.
- 30d usage: 3.
- risk: high — changes care data and notification behavior.
- bugs/tickets: verify care-plan field validation.

### `update_client_phone_preference`

- registry: `libraries/python/phoebe_event_agent/tools/client_record.py:256`
- purpose/surface: change client call-phone preference; in-app chat.
- gate: organization client ownership and approval policy.
- payload: required `client_id`, `client_call_phone_preference`.
- confirmation: single-shot approval.
- db: `app.org_client_records` U; one row.
- mode_context: yes.
- external: activity event.
- 30d usage: 0.
- risk: medium — changes contact routing.
- bugs/tickets: source audit reports zero use; verify runtime mount.

### `assign_client_care_coordinator`

- registry: `libraries/python/phoebe_event_agent/tools/client_record.py:390`
- purpose/surface: assign a care coordinator to a client; in-app chat.
- gate: organization membership for both records and approval policy.
- payload: required `client_id`, `care_coordinator_id`.
- confirmation: single-shot approval.
- db: `app.org_care_coordinator_clients` C; one link row.
- mode_context: yes.
- external: activity event.
- 30d usage: 0.
- risk: medium — changes ownership routing.
- bugs/tickets: source audit reports zero use; verify runtime mount.

### `remove_client_care_coordinator`

- registry: `libraries/python/phoebe_event_agent/tools/client_record.py:478`
- purpose/surface: remove a client/coordinator link; in-app chat.
- gate: organization membership and approval policy.
- payload: required `client_id`, `care_coordinator_id`.
- confirmation: single-shot approval.
- db: `app.org_care_coordinator_clients` D; one link row.
- mode_context: yes.
- external: activity event.
- 30d usage: 0.
- risk: medium — changes ownership routing.
- bugs/tickets: source audit reports zero use.

### `approve_staged_message`

- registry: `libraries/python/phoebe_event_agent/tools/conversation_messaging.py:185`
- purpose/surface: approve and send a staged caregiver message; in-app chat.
- gate: staged-message ownership and approval state; always-allow rules can bypass a card only where policy permits.
- payload: required `conversation_id`, `staged_message_id`, `updated_content`.
- confirmation: preview/confirm; this is the confirm step.
- db: `app.staged_messages` U; one staged row, then the message sender writes conversation/message rows downstream.
- mode_context: yes for conversation scope.
- external: SMS/message provider through the staged-message sender.
- 30d usage: 0.
- risk: high — sends customer-facing content.
- bugs/tickets: source audit reports zero use; verify send idempotency.

### `reject_staged_message`

- registry: `libraries/python/phoebe_event_agent/tools/conversation_messaging.py:251`
- purpose/surface: reject a staged message; in-app chat.
- gate: staged-message ownership and approval state.
- payload: required `conversation_id`, `staged_message_id`; optional `reason`.
- confirmation: single-shot rejection.
- db: `app.staged_messages` U; one row marked rejected.
- mode_context: yes.
- external: optional Slack notification for staged-message edits.
- 30d usage: 6.
- risk: medium — suppresses a pending customer message.
- bugs/tickets: verify rejected-content retention.

### `edit_staged_message_content`

- registry: `libraries/python/phoebe_event_agent/tools/conversation_messaging.py:358`
- purpose/surface: edit staged message text before approval; in-app chat.
- gate: staged-message ownership and edit policy.
- payload: required `conversation_id`, `staged_message_id`, `updated_content`.
- confirmation: preview/confirm; edit is not send.
- db: `app.staged_messages` U; one row.
- mode_context: yes.
- external: Slack edit notification.
- 30d usage: 0.
- risk: medium — changes pending customer content.
- bugs/tickets: source audit reports zero use.

### `send_one_off_sms_in_conversation`

- registry: `libraries/python/phoebe_event_agent/tools/conversation_messaging.py:468`
- purpose/surface: send an immediate one-off SMS to a caregiver; in-app chat.
- gate: caregiver and conversation ownership; `send_anyway` is required after warning confirmation.
- payload: required `conversation_id`, `caregiver_id`, `content`; optional `target_language`, `send_anyway`.
- confirmation: preview/warning then confirm by re-call with `send_anyway=true`.
- db: sender helper writes conversation/message/contact-attempt rows; exact tables are hidden by `send_one_off_sms_batch`, needs manual verification. No direct SQL in this tool.
- mode_context: yes through conversation context.
- external: Twilio/SMS provider.
- 30d usage: 4,389.
- risk: high — sends external caregiver communication.
- bugs/tickets: verify helper idempotency and row footprint.

### `place_one_off_voice_call_in_conversation`

- registry: `libraries/python/phoebe_event_agent/tools/conversation_messaging.py:614`
- purpose/surface: place an immediate one-off caregiver call; in-app chat.
- gate: caregiver and conversation ownership; warning confirmation can require `send_anyway`.
- payload: required `conversation_id`, `caregiver_id`, `instructions`; optional `opening_line`, `send_anyway`.
- confirmation: preview/warning then confirm.
- db: call-attempt/helper rows are hidden by the call service; needs manual verification.
- mode_context: yes through conversation context.
- external: voice provider/Twilio call creation.
- 30d usage: 804.
- risk: high — starts an external call.
- bugs/tickets: verify provider failure rollback and attempt row count.

## workflow and temporal

### `trigger_ehr_sync`

- registry: `libraries/python/phoebe_event_agent/tools/data_sync.py:50`
- purpose/surface: enqueue a client or caregiver EHR sync; in-app chat.
- gate: organization entity ownership and approval policy.
- payload: required `entity_type`; optional `client_id`, `caregiver_id`.
- confirmation: single-shot approval.
- db: no direct write; queue message only. The sync worker updates the linked EHR and may update `app.org_client_records` or `app.org_caregiver_records` after reconciliation.
- mode_context: yes in context; downstream mode needs manual verification.
- external: queue publish and EHR API.
- 30d usage: 422.
- risk: high — changes external source-of-truth records.
- bugs/tickets: coworker audit correctly notes this does not itself write the EHR.

### `draft_agent_outreach`

- registry: `libraries/python/phoebe_event_agent/tools/scheduling.py:366`
- purpose/surface: create a pending outreach draft; in-app chat.
- gate: organization outreach feature and approval policy. `auto_start` is not an approval bypass.
- payload: required `shift_ids`; optional caregiver suggestions, outreach type/urgency, reason, instructions, schedule, grouping, auto-assign fields, and care-plan data.
- confirmation: preview/draft first; live start requires a separate start tool unless explicitly configured by the current flow.
- db: `app.outreaches` C; `app.outreach_shifts` C; `app.outreach_subscribers` C; `app.phoebe_agent_run_events` C; one outreach plus one link/subscriber/event set per invocation. Failure cleanup deletes the draft.
- mode_context: yes; organization-scoped.
- external: kickoff events and optional Temporal scheduling.
- 30d usage: 3,965.
- risk: high — creates durable outreach state and can schedule future work.
- bugs/tickets: source audit and current code disagree on autonomous `auto_start`; current code requires runtime-policy verification.

### `manage_outreach_start_with_contacts`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:702`
- purpose/surface: start an outreach and contact selected caregivers; in-app chat.
- gate: `has_outreach_sending`, bound organization outreach, exact caregiver/shift conflict acknowledgement, and approval card unless always allowed.
- payload: required `outreach_id`, `caregiver_shift_ids`; optional `acknowledged_conflicts`, `acknowledged_rejections`, delivery mode, and contact overrides.
- confirmation: preview/confirm; this is the live start step.
- db: `app.outreaches` U; `app.contact_attempts` C; `app.contact_attempt_shift_responses` C; `app.outreach_shifts` U; `app.phoebe_agent_run_events` C. Rows scale with caregiver/shift pairs.
- mode_context: yes.
- external: queue publishes and Temporal callout workflow poke.
- 30d usage: 1,928.
- risk: high — starts live calls/SMS to caregivers.
- bugs/tickets: verify exact attempt dedupe and delivery-mode behavior.

### `manage_outreach_add_contact_attempts`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:930`
- purpose/surface: add contact attempts to an active outreach; in-app chat.
- gate: `has_outreach_sending`, active outreach, conflict/rejection acknowledgement, and approval policy.
- payload: required `outreach_id`, `caregiver_ids`, `shift_ids`; optional delivery mode, acknowledgements, and exclusions.
- confirmation: preview/confirm.
- db: `app.contact_attempts` C; `app.contact_attempt_shift_responses` C; `app.outreach_shifts` U; `app.phoebe_agent_run_events` C. One attempt per selected caregiver and response row per selected shift.
- mode_context: yes.
- external: queue publish and Temporal workflow poke.
- 30d usage: 7,200.
- risk: high — starts new external contacts.
- bugs/tickets: verify ad hoc retry limits.

### `add_outreach_suggestions`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:231`; sidebar duplicate `libraries/python/phoebe_event_agent/tools/sidebar_outreach/suggestion_tools.py:1397`
- purpose/surface: add or overwrite caregiver suggestions on an outreach; in-app chat.
- gate: organization outreach ownership and approval policy.
- payload: required `outreach_id`, `suggestions`; each suggestion requires `caregiver_id` and may include covered shifts/groups; optional `mode`, acknowledgements.
- confirmation: single-shot approval; no external contact until start.
- db: `app.outreaches` U; suggestion JSON/relationships change on one outreach row.
- mode_context: yes.
- external: activity event.
- 30d usage: 12,557.
- risk: medium — changes who a later outreach may contact.
- bugs/tickets: duplicate registry mounts can diverge; verify the active runtime group.

### `manage_outreach_remove_suggestions`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:458`; sidebar duplicate `libraries/python/phoebe_event_agent/tools/sidebar_outreach/suggestion_tools.py:1900`
- purpose/surface: remove caregiver suggestions from an outreach; in-app chat.
- gate: organization outreach ownership and approval policy.
- payload: required `outreach_id`, `caregiver_ids`.
- confirmation: single-shot approval.
- db: `app.outreaches` U; one outreach suggestion list update.
- mode_context: yes.
- external: activity event.
- 30d usage: 2,370.
- risk: medium — removes candidate coverage.
- bugs/tickets: verify duplicate registry mount behavior.

### `manage_outreach_set_status`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:556`
- purpose/surface: pause, resume, or cancel an outreach; in-app chat.
- gate: organization outreach ownership, allowed transition, and approval policy.
- payload: required `outreach_id`, `action`.
- confirmation: single-shot approval.
- db: `app.outreaches` U; one status row update; cancellation may create `app.organization_events` and action-needed updates.
- mode_context: yes.
- external: Temporal cancel or poke signal.
- 30d usage: 1,120.
- risk: high — stops or resumes live outreach.
- bugs/tickets: verify cancellation signal after transaction commit.

### `update_agent_outreach`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:1132`
- purpose/surface: edit outreach shifts, grouping, reason, care plans, or auto-assign settings; in-app chat.
- gate: organization ownership; shift structure edits require pending status; approval policy applies.
- payload: required `outreach_id`; optional `shift_ids`, `shift_groups`, auto-assign flags, reason, recurrence text, and shift care plans.
- confirmation: preview when changing caregiver-facing fields; confirm outside this tool for re-notify.
- db: `app.outreaches` U; `app.outreach_shifts` C/U/D as shift membership changes; possibly `app.phoebe_agent_run_events` C. Rows scale with changed shifts.
- mode_context: yes.
- external: Temporal poke and optional EHR/notification follow-up.
- 30d usage: 620.
- risk: high — changes live outreach structure.
- bugs/tickets: current code states chat-side re-notify confirm is separate scope.

### `update_outreach_metadata`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_metadata.py:50`
- purpose/surface: edit outreach instructions and message templates; in-app chat.
- gate: organization outreach ownership and approval policy.
- payload: required `outreach_id`; optional instruction, opening line, templates, and `notify_client_on_fill`.
- confirmation: single-shot approval.
- db: `app.outreaches` U; one metadata JSON update.
- mode_context: yes.
- external: activity event.
- 30d usage: 964.
- risk: medium — changes future caregiver-facing messages.
- bugs/tickets: verify template validation for each outreach type.

### `fill_callout_shifts`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_fill.py:228`
- purpose/surface: assign one caregiver to one or more outreach shifts; in-app chat.
- gate: organization/outreach ownership, fill safety checks, approval policy, and explicit EHR propagation option.
- payload: required `callout_id`, `shift_ids`, `caregiver_id`; optional notification, EHR propagation, rate selections, `assign_without_response`, and coordinator request.
- confirmation: preview/confirm; EHR writes occur only after approval and safety checks.
- db: `app.shifts` U; `app.outreach_shifts` U; `app.contact_attempt_shift_responses` U/C; `app.phoebe_agent_run_events` C. One shift assignment per selected shift.
- mode_context: yes.
- external: EHR API, queue notifications, and activity events.
- 30d usage: 541.
- risk: high — assigns work and may write an external EHR.
- bugs/tickets: verify partial-failure rollback between Phoebe and EHR.

### `notify_caregivers`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_management.py:670`
- purpose/surface: notify caregivers that outreach shifts were handled; in-app chat.
- gate: organization outreach ownership and approval policy.
- payload: required `outreach_id`, `caregiver_shift_ids`; optional custom assigned/unassigned messages and inclusion flags.
- confirmation: single-shot approval.
- db: no direct domain row write; notification workflow may create `app.outreach_notification_messages` and message/attempt rows.
- mode_context: yes in payload.
- external: Temporal `ShiftHandledNotifyWorkflow`, SMS/voice notifications.
- 30d usage: 403.
- risk: high — sends caregiver-facing messages.
- bugs/tickets: needs manual verification of workflow-created rows.

### `notify`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_compat.py:57`
- purpose/surface: send a coordinator-authored outreach notification; in-app chat.
- gate: organization outreach ownership and approval policy.
- payload: required `outreach_id`, `title`, `message`, `level`.
- confirmation: single-shot approval.
- db: `app.organization_events` C; one event row. Notification delivery is handled by the event path.
- mode_context: yes.
- external: Slack/UI notification side effects.
- 30d usage: 1,604.
- risk: medium — broadcasts an operator message.
- bugs/tickets: source audit calls this a compatibility tool; verify duplicate notification suppression.

### `override_contact_attempt_response`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_review.py:84`
- purpose/surface: correct a caregiver response after review; in-app chat.
- gate: organization ownership, attempt/shift validation, and approval policy.
- payload: required `contact_attempt_id`, `shift_id`, `response`.
- confirmation: single-shot approval.
- db: `app.contact_attempt_shift_responses` U/C; one response row for the selected attempt/shift. May update `app.contact_attempts` outcome.
- mode_context: yes.
- external: memory compile or Slack review notification only when the corrected response creates a note.
- 30d usage: 225.
- risk: high — changes a recorded caregiver decision.
- bugs/tickets: verify audit trail for manual overrides.

### `submit_outreach_suggestion_feedback`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_review.py:314`
- purpose/surface: record why a caregiver suggestion was accepted or rejected; in-app chat.
- gate: organization/outreach ownership and approval policy.
- payload: required `outreach_id`, `caregiver_id`, `sentiment`, `reason`.
- confirmation: single-shot.
- db: `app.org_caregiver_memories`/`app.org_caregiver_memory_fragments` may be C when feedback becomes memory; exact branch is conditional.
- mode_context: yes.
- external: Slack feedback notification and memory compile.
- 30d usage: 1.
- risk: medium — can persist feedback as durable memory.
- bugs/tickets: needs manual verification of the positive/negative branch.

### `add_outreach_subscriber`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_review.py:762`
- purpose/surface: subscribe a user to outreach updates; in-app chat.
- gate: organization membership and outreach ownership.
- payload: required `outreach_id`, `user_id`.
- confirmation: single-shot approval.
- db: `app.outreach_subscribers` C; one link row.
- mode_context: yes.
- external: notification routing.
- 30d usage: 0.
- risk: low — changes notification recipients.
- bugs/tickets: source audit reports zero use.

### `remove_outreach_subscriber`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_review.py:846`
- purpose/surface: remove an outreach subscriber; in-app chat.
- gate: organization membership and outreach ownership.
- payload: required `outreach_id`, `user_id`.
- confirmation: single-shot approval.
- db: `app.outreach_subscribers` D or `app.outreach_unsubscribes` C, depending helper state.
- mode_context: yes.
- external: notification routing.
- 30d usage: 0.
- risk: low — changes notification recipients.
- bugs/tickets: verify sticky unsubscribe behavior.

### `monitor_outreach`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_monitoring.py:132`
- purpose/surface: schedule a monitored outreach run; in-app chat.
- gate: active organization outreach and agent-run scope; approval policy applies.
- payload: required `outreach_id`, `reason`, `conditions`; optional `pre_approved_actions`.
- confirmation: single-shot approval; monitoring itself is the durable action.
- db: `app.phoebe_agent_runs` U; `app.scheduled_monitoring` C; one monitoring schedule per invocation, with run metadata update.
- mode_context: yes.
- external: Temporal/self-poke scheduling.
- 30d usage: 2,587.
- risk: high — schedules autonomous follow-up actions.
- bugs/tickets: verify pre-approved action enforcement against current tool policy.

### `stop_agent_monitoring`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_monitoring.py:271`
- purpose/surface: stop a monitored outreach; in-app chat.
- gate: active run and organization ownership.
- payload: required `outreach_id`, `reason`.
- confirmation: single-shot approval.
- db: `app.scheduled_monitoring` D/U and `app.phoebe_agent_runs` U; one schedule is stopped.
- mode_context: yes.
- external: cancels scheduled follow-up.
- 30d usage: 435.
- risk: medium — stops autonomous follow-up.
- bugs/tickets: verify already-fired workflow cancellation.

### `schedule_self_poke`

- registry: `libraries/python/phoebe_event_agent/tools/scheduling.py:1922`
- purpose/surface: schedule a delayed follow-up for the current run; in-app chat.
- gate: interactive run, self-poke governor, and max pending-poke limits.
- payload: required `delay_minutes`, `reason`; optional `instructions`, `pre_approved_actions`.
- confirmation: single-shot approval; the scheduled action is not an external caregiver message by itself.
- db: `app.phoebe_agent_run_pending_events` C; one pending event per invocation, plus run event bookkeeping.
- mode_context: yes through agent-run context.
- external: queue/Temporal wake-up.
- 30d usage: source audit's legacy `schedule_self_poke` count was 2,008.
- risk: medium — schedules future agent activity.
- bugs/tickets: coworker audit calls the newer equivalent `schedule_poke`; current code exposes `schedule_self_poke`.

### `assign_shifts_to_caregiver`

- registry: `libraries/python/phoebe_event_agent/tools/schedule.py:1251`
- purpose/surface: assign selected shifts to a caregiver; in-app chat.
- gate: organization ownership, assignment safety, and approval policy.
- payload: required `shift_ids`, `caregiver_id`; optional reassignment and rate fields.
- confirmation: preview/confirm when conflicts exist.
- db: `app.shifts` U; `app.shift_progressions` C; `app.outreach_shifts` U when an active outreach exists. One assignment per shift.
- mode_context: yes.
- external: EHR writeback and notifications when configured.
- 30d usage: 127.
- risk: high — assigns live work and may write an EHR.
- bugs/tickets: verify assignment propagation across grouped shifts.

### `create_shift`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:233`
- purpose/surface: create a new shift in needs-coverage state; in-app chat.
- gate: `has_create_shift` feature flag, organization context, and approval policy.
- payload: required `patient_name`, `shift_start_time`, `shift_end_time`, `client_id`; optional address, frequency, notes, and assignment fields.
- confirmation: preview/confirm.
- db: `app.shifts` C; one shift row and one initial progression/audit row.
- mode_context: yes.
- external: optional EHR sync and activity event.
- 30d usage: 0.
- risk: high — creates a schedulable shift.
- bugs/tickets: source audit reports zero use; verify current feature flag exposure.

### `update_shift`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:403`
- purpose/surface: edit shift time, patient-facing details, notes, or assignment; in-app chat.
- gate: organization ownership, editable-field allowlist, and approval policy.
- payload: required `shift_id`; optional times, patient fields, notes, `unassign_caregiver`, and `writeback_scope`.
- confirmation: preview/confirm for caregiver-facing changes.
- db: `app.shifts` U; `app.shift_progressions` C for assignment/status changes; one shift update.
- mode_context: yes.
- external: EHR time/assignment writeback and optional notification preview.
- 30d usage: 852.
- risk: high — changes live schedule data.
- bugs/tickets: current code says re-notify confirmation is a separate future scope.

### `add_shift_note_to_ehr`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:831`
- purpose/surface: append a shift note to the linked EHR; in-app chat.
- gate: note-writeback flag, source-system safety checks, and explicit coordinator approval.
- payload: required `shift_id`, `note`; optional `as_activity_note`, `expected_source_system`.
- confirmation: preview/confirm.
- db: `app.shift_notes` C; one local note/audit row. The external EHR note is separate.
- mode_context: yes.
- external: AxisCare/WellSky EHR API; local refresh after write.
- 30d usage: 10.
- risk: high — writes an external clinical/workforce record.
- bugs/tickets: source audit lists the WellSky alias `add_shift_note_to_wellsky`; current code uses one registered tool.

### `cancel_shift`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:1165`
- purpose/surface: cancel one shift; in-app chat.
- gate: one shift per call, explicit coordinator approval, EHR safety checks.
- payload: required `shift_id`, `cancelled_by`; optional `reason`, client/shift display fields.
- confirmation: preview/confirm.
- db: `app.shifts` U; `app.shift_progressions` C; clock-reminder rows/events are cancelled. One shift per invocation.
- mode_context: yes.
- external: EHR cancellation before local commit, plus notification/workflow cancellation.
- 30d usage: 12.
- risk: high — cancels scheduled care.
- bugs/tickets: verify EHR-first rollback guarantee.

### `update_shift_triage_stage`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:1577`
- purpose/surface: append a triage stage to a shift; in-app chat.
- gate: assigned caregiver match, allowed stage list, and approval policy. Clocked stages are rejected.
- payload: required `shift_id`, `caregiver_id`, `stage`; optional `reason`.
- confirmation: single-shot approval.
- db: `app.shift_progressions` C; one progression row.
- mode_context: yes.
- external: activity event.
- 30d usage: 7.
- risk: medium — changes operational status.
- bugs/tickets: verify stage allowlist remains aligned with UI.

### `cancel_callout_shifts`

- registry: `libraries/python/phoebe_event_agent/tools/shift_management.py:1755`
- purpose/surface: remove shifts from an active multi-shift outreach; in-app chat.
- gate: active outreach, exact shift list, notification acknowledgement, and approval policy.
- payload: required `outreach_id`, `shift_ids`; optional notifications, removal/cancellation reasons, and non-responder inclusion.
- confirmation: preview/confirm.
- db: `app.outreaches` U; `app.outreach_shifts` U/D; `app.shifts` U; `app.organization_events` C; one set of rows per selected shift.
- mode_context: yes.
- external: Temporal workflow cancel, notifications, and optional EHR memo writeback.
- 30d usage: 257.
- risk: high — cancels work inside a live outreach.
- bugs/tickets: verify partial cancellation and auto-cancel transition.

## feature flags and settings

### `update_organization_general_settings`

- registry: `libraries/python/phoebe_event_agent/tools/organization_general_settings.py:375`
- purpose/surface: update allowlisted organization reply settings; in-app chat.
- gate: `has_chat_settings_editing`; allowlisted fields only; approval card unless always allowed.
- payload: one or more optional boolean fields: `auto_send_callout_decline_response`, positive/negative/unclear shift-confirmation replies, and `auto_send_all_replies`.
- confirmation: preview/confirm settings card.
- db: `app.organization_general_settings` U; one organization row.
- mode_context: yes.
- external: organization settings update event.
- 30d usage: 1.
- risk: high — changes automatic customer messaging.
- bugs/tickets: source audit calls this specialized tool `write_settings`; current code uses the allowlisted settings tool.

### `update_callout_settings`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_settings.py:258`
- purpose/surface: update callout auto-assign and outreach defaults; in-app chat.
- gate: settings-edit feature and approval policy.
- payload: optional `auto_assign_first_responder_default`, `allow_auto_assign`, `skip_busy_caregivers`.
- confirmation: preview/confirm.
- db: `app.callout_settings` U; one organization row.
- mode_context: yes.
- external: settings update event.
- 30d usage: 0.
- risk: high — changes future outreach behavior.
- bugs/tickets: source audit reports zero use.

### `update_callout_excluded_caregivers`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_settings.py:389`; sidebar mount `libraries/python/phoebe_event_agent/tools/sidebar_outreach/outreach_exclusion_tools.py:36`
- purpose/surface: add or remove caregivers from callout exclusion settings; in-app chat.
- gate: outreach settings permission and approval policy.
- payload: required `action`, `caregiver_ids`.
- confirmation: single-shot approval.
- db: `app.callout_settings` U or exclusion relation rows C/D; one organization setting plus one row per caregiver as applicable.
- mode_context: yes.
- external: settings update event.
- 30d usage: 2.
- risk: high — changes who can receive outreach.
- bugs/tickets: duplicate mounts need runtime verification.

### `update_recommendation_defaults`

- registry: `libraries/python/phoebe_event_agent/tools/recommendation_defaults.py:209`
- purpose/surface: change default caregiver recommendation filters; in-app chat.
- gate: organization settings permission and approval policy.
- payload: optional bounded fields including distance, hours, consecutive days, overtime, commute, tag, office, and ignored tags.
- confirmation: preview/confirm.
- db: `app.recommendation_defaults` C/U; one organization row.
- mode_context: yes.
- external: settings update event.
- 30d usage: 6.
- risk: medium — changes future matching recommendations.
- bugs/tickets: verify create-versus-update branch.

### `set_tag_matching_importance`

- registry: `libraries/python/phoebe_event_agent/tools/tag_overrides.py:45`
- purpose/surface: set the importance of a client tag for matching; in-app chat.
- gate: organization/client ownership and approval policy.
- payload: required `tag`, `importance`; optional `client_id`.
- confirmation: single-shot approval.
- db: `app.org_client_records` U or tag override JSON; one client/organization row.
- mode_context: yes.
- external: recommendation cache invalidation.
- 30d usage: 0.
- risk: medium — changes recommendation results.
- bugs/tickets: source audit reports zero use; verify exact column/helper table.

### `apply_reliability_policy_request_tool`

- registry: `libraries/python/phoebe_event_agent/tools/playbook_rules.py:292`
- purpose/surface: turn a natural-language reliability request into a playbook change request; in-app chat.
- gate: playbook surface and approval policy.
- payload: required `expected_version`, `request_text`.
- confirmation: preview/confirm with optimistic version check.
- db: `app.agent_playbook_item_activity` C and possibly `app.agent_playbook_refused_requests` C; no direct item mutation until the edit tool.
- mode_context: yes.
- external: none.
- 30d usage: needs manual verification; source audit did not list it.
- risk: medium — creates a policy-change request.
- bugs/tickets: current code discovery; verify whether this tool is mounted on customer-facing runs.

### `update_playbook_item_tool`

- registry: `libraries/python/phoebe_event_agent/tools/playbook_rules.py:419`
- purpose/surface: edit a playbook item; in-app chat.
- gate: playbook permission and approval policy.
- payload: required `playbook_id`, `expected_version`; optional title, usage text, instructions, surfaces, enabled.
- confirmation: preview/confirm with version check.
- db: `app.agent_playbook_items` U; `app.agent_playbook_item_activity` C; one item and one audit row.
- mode_context: yes.
- external: prompt/cache invalidation.
- 30d usage: 32.
- risk: high — changes agent policy.
- bugs/tickets: verify customer-facing mount versus internal playbook editor.

### `apply_playbook_natural_language_edit`

- registry: `libraries/python/phoebe_event_agent/tools/playbook_rules.py:605`
- purpose/surface: apply a natural-language playbook edit; in-app chat.
- gate: playbook permission, expected current version, and approval policy.
- payload: required `playbook_id`, `request_text`.
- confirmation: preview/confirm.
- db: `app.agent_playbook_items` U; `app.agent_playbook_item_activity` C; one item and audit row.
- mode_context: yes.
- external: prompt/cache invalidation.
- 30d usage: 217.
- risk: high — changes agent policy from natural language.
- bugs/tickets: verify version conflict handling.

## other side effects

### `update_outreach_filter_rule`

- registry: `libraries/python/phoebe_event_agent/tools/outreach_filter_rules.py:235`
- purpose/surface: add, replace, or remove an outreach filter rule; in-app chat.
- gate: organization outreach settings permission and approval policy.
- payload: required `action`, `outreach_type`, `entity_type`; optional `entity_id`, `entity_tag`, `filter_mode`, `exception_entity_ids`.
- confirmation: single-shot approval.
- db: `app.outreach_filter_rules` C/U/D; one rule for upsert/delete, plus `app.organization_events` C.
- mode_context: yes.
- external: filter-cache invalidation.
- 30d usage: 328.
- risk: medium — changes who qualifies for future outreach.
- bugs/tickets: verify entity-name resolution does not widen organization scope.

### `submit_general_agent_feedback`

- registry: `libraries/python/phoebe_event_agent/tools/general_agent_feedback.py:46`
- purpose/surface: record feedback about an agent run; in-app chat.
- gate: authenticated user and current run context.
- payload: required `message`; optional `category`.
- confirmation: single-shot.
- db: `app.phoebe_agent_run_feedback` C; one feedback row.
- mode_context: yes through the run organization.
- external: Slack escalation for selected categories.
- 30d usage: 39.
- risk: low — writes feedback, not customer state.
- bugs/tickets: verify PII filtering in Slack escalation.

### `generate_report`

- registry: `libraries/python/phoebe_event_agent/tools/generate_report.py:63`
- purpose/surface: persist or render a downloadable report artifact; in-app chat.
- gate: current agent run and report size limits.
- payload: optional `report_id`, `title`, `columns`, `rows`; manual rows are bounded.
- confirmation: single-shot artifact creation.
- db: report-handle path reads `app.phoebe_agent_run_events`/run state; manual table has no DB write. A report artifact may be stored in `app.phoebe_agent_run_event_items` by the run-state helper; needs manual verification.
- mode_context: yes through the run.
- external: artifact/storage side effect.
- 30d usage: 48.
- risk: low — creates an export artifact without changing business rows.
- bugs/tickets: verify artifact table versus object storage path.

### `get_outreach_suggestion_drafts`

- registry: `libraries/python/phoebe_event_agent/tools/fast_outreach_drafts.py:150` (wrapper over `sidebar_outreach/recommendation_tools.py:4699`)
- purpose/surface: calculate and store ranked caregiver suggestion drafts; in-app chat.
- gate: existing outreach, active agent run, and recommendation policy.
- payload: optional `outreach_id`, `min_options_per_unit`, `limit`, `max_suggestions_per_unit`, weights, tags, languages, and gender filters.
- confirmation: preview only; no live contact.
- db: `app.phoebe_agent_run_event_items` or run-scoped draft storage C; exact helper table needs manual verification. The wrapper explicitly says the draft is stored for the apply tool.
- mode_context: yes.
- external: recommendation computation only.
- 30d usage: source audit did not separate this newer name from `add_outreach_suggestions`; needs manual verification.
- risk: medium — prepares durable candidate choices.
- bugs/tickets: current code discovery; source audit uses older suggestion names.

### `apply_outreach_suggestion_drafts`

- registry: `libraries/python/phoebe_event_agent/tools/fast_outreach_drafts.py:184` (wrapper over `sidebar_outreach/suggestion_tools.py:1121`)
- purpose/surface: apply the latest ranked suggestion draft; in-app chat.
- gate: existing outreach, active run, conflict acknowledgements, and approval policy.
- payload: optional `outreach_id`, `mode` (`overwrite` or `append`), `draft_id`.
- confirmation: preview/confirm.
- db: `app.outreaches` U; `app.outreach_shifts` C/U/D as suggestions change; `app.phoebe_agent_run_event_items` C for audit. Rows scale with suggestions.
- mode_context: yes.
- external: activity events; no caregiver contact until a start tool runs.
- 30d usage: needs manual verification.
- risk: medium — changes later outreach recipients.
- bugs/tickets: current code discovery; verify draft expiry and stale-version handling.

### `merge_recommendation_fork_suggestion`

- registry: `libraries/python/phoebe_event_agent/tools/sidebar_outreach/suggestion_tools.py:1397`
- purpose/surface: merge a recommendation fork into outreach suggestions; in-app chat.
- gate: outreach-bound sidebar context, conflict acknowledgement, and approval policy.
- payload: required `suggestions`; optional `mode`, `acknowledged_conflicts`, `acknowledged_rejections`.
- confirmation: preview/confirm.
- db: `app.outreaches` U and `app.outreach_shifts` C/U/D; `app.phoebe_agent_run_event_items` C. Rows scale with the merged suggestion set.
- mode_context: yes.
- external: recommendation audit events.
- 30d usage: needs manual verification.
- risk: medium — changes candidate coverage.
- bugs/tickets: current code discovery; verify this tool is not mounted twice under the same name.

### `post_to_slack_thread` and `schedule_followup` — excluded from count

- registry: `libraries/python/phoebe_slack/slack_agent_tools.py` and `slack_automation_tools.py`.
- reason: these write internal staff Slack state, not the customer-facing SMS, voice, or caregiver chat surface requested here. The first source audit includes them in its broader coordinator inventory.
- side effects: Slack posts and scheduled automation state.
- status: excluded by corrected scope; manual verification is not needed unless Henry re-expands scope.

## post-call voice write actions

These actions are customer-agent side effects, but current code does not expose
them as LLM tools. They run after the call and are not included in the 90-tool
registration count.

| action | registry path | direct footprint | external effect | 30d usage |
|---|---|---|---|---:|
| clock-in writeback | `services/worker/handlers/voice/qa_voice_agent_actions.py` | `app.shifts` U; `app.shift_progressions` C | EHR clock-in API | 586 historical tool uses |
| clock-out writeback | same file | `app.shifts` U; `app.shift_progressions` C | EHR clock-out API | 492 historical tool uses |
| reminder bookkeeping | same file | `app.contact_attempts` U | none | included in reminder counts |
| wrong availability cleanup | same file | deletes `llm_tool` availability rows from `app.org_caregiver_availability` | none | needs manual verification |
| receptionist item capture | `services/worker/handlers/voice/after_hours_receptionist_generate.py` | receptionist item table C | Slack/notification path | 3,367 historical uses |
| voice memory generation | post-call memory handler | caregiver memory and fragment rows C/U | memory compile queue | needs manual verification |

## reconciliation appendix

| source disagreement or gap | code-grounded result |
|---|---|
| user correction says the coworker file audits v2 customer-facing, despite its v3 filename | use it only as the second customer-facing source; do not include admin or v3 internal agents |
| requested `libraries/python/llm_messaging_engine` path | absent from current `origin/main`; current code uses `libraries/python/sms_conversation_engine` |
| source audit says SMS has `prepare_shift_cancellation_selection_tool` as a write | current code only reads and returns selection state; it is excluded from the write count |
| source audit lists `offer_open_shifts_tool` as available | current registration exists, but the source reports zero uses and runtime feature gating can exclude it |
| source audit lists voice clock tools as live agent tools | current `clock_in_agent.py` and `clock_out_agent.py` return empty scenario groups; post-call QA owns clock writes |
| source audit lists `capture_receptionist_item` as a voice tool | current live registry has no such LLM registration; post-call worker owns it |
| source audit lists `add_shift_note_to_wellsky` as an alias | current code has one `add_shift_note_to_ehr` registration with source-system arguments |
| source audit calls settings a generic `write_settings` tool | current code has `update_organization_general_settings` plus specialized callout and recommendation settings tools |
| coworker audit calls the delayed tool `schedule_poke` | current code registers `schedule_self_poke`; treat them as the same workflow family, not the same current name |
| coworker audit lists `send_sms`, `place_voice_call`, and staged-message tools as separate current names | current registry uses `send_one_off_sms_in_conversation`, `place_one_off_voice_call_in_conversation`, and three staged-message tools |
| coworker audit says `watch` is read-only SQL | current `monitor_outreach` creates scheduled monitoring state and is a write tool |
| coworker audit says `trigger_ehr_sync` does not write the EHR | code confirms the tool only publishes a queue message; the worker performs the external write |
| source audit lists `surface_organization_setting` as a write | current function renders a settings card; it does not update a DB row, so it is excluded |
| source audit lists `mark_conversation_complete` and `record_intent` with write tools | current code has no business-table write; both remain listed because they persist or control engine side effects outside the function |
| source audit includes internal Slack post and follow-up tools under in-app | corrected scope excludes staff-only Slack writes; caregiver-facing `send_one_off_sms_in_conversation` remains included |

## manual verification queue

- verify the missing `llm_messaging_engine` package name with the repository owner.
- trace the hidden helper used by one-off SMS and voice calls to exact attempt/message tables.
- trace survey transition API mode propagation and exact preference tables.
- trace shift-discovery worker row counts for offer, response, and interest.
- confirm current runtime mounts for newer fast-outreach draft tools.
- confirm report artifact storage table or object-store path.
- verify post-call voice action usage and exact table rows.

The code-grounded count is 90 registered customer-facing write tools: 17 SMS,
14 voice, and 59 in-app. The count excludes internal admin/v3 agents, control
only tools, read-only tools, and staff-only Slack writes.
