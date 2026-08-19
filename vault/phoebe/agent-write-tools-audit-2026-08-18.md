---
type: reference
tags: [phoebe]
created: 2026-08-18
updated: 2026-08-18
---

# Phoebe Agent Write Tools Audit (2026-08-18)

> Copied from repo doc docs/notes/phoebe_agent_write_tools_audit_2026_08_18.md (origin/main @ 97a66ecf29); repo copy is canonical for code refs.


Date: 2026-08-18. Code state: `origin/main` @ `97a66ecf29`.

Scope: the customer-facing Phoebe agent across all three surfaces —

1. **SMS conversation engine** (`libraries/python/sms_conversation_engine`) — caregiver-facing texting
2. **Voice agents** (`services/voice`) — caregiver/client-facing calls
3. **In-app coordinator agent** (`libraries/python/phoebe_event_agent`) — the chat sidebar + outreach-analysis agent coordinators use in the web app; also runs headless automations. This is the direct predecessor of the v3 agent.

Explicitly excluded: `phoebe_v3_agent` and the internal admin agent.

Usage data: production analytics read replica, trailing 30 days
(2026-07-19 → 2026-08-18).

- SMS: `function_call` items in `app.sms_engine_runs.llm_items` (167,403 runs). Caveat: table has no `mode` column, so counts include any sandbox runs (negligible).
- Voice: `kind='tool_start'` entries in `app.voice_calls.developer_transcript` (`mode='live'`, `is_demo=false`; 377,837 calls).
- In-app: `type='function_call'` rows in `app.agent_items` joined through `agent_turns` → `agent_conversations` (`mode='live'`, `conversation_type IN ('general','outreach_analysis')`; 24,502 conversations, 170,646 tool calls — 137,754 outreach_analysis + 32,892 general).

---

## 1. How tools are exposed

1. **SMS engine** — `build_engine_tool_group()`
   (`sms_conversation_engine/adapter.py:2946`) composes one tool set per
   turn: outreach + intent + shift search + clock reminder response tools
   always; clock in/out tools gated per-org (`clock_in_writes_enabled` /
   `clock_out_writes_enabled`); shift cancellation tools gated by snapshot;
   care-plan detail tool gated by per-contact-attempt availability; shift
   discovery gated by `shift_discovery_enabled`. Preference-survey
   conversations get only the survey tool; the admin simulator passes a
   dry-run override group.
2. **Voice agents (cascade pipeline)** — each agent class in
   `services/voice/agents/` returns scenario tools, plus `core_tools`
   (`end_call`, `transfer_call`, `mark_as_voicemail`) always
   (`base.py:531`).
3. **Voice flows** — `services/voice/flows/` is a deterministic,
   delivery-gated state machine (Pipecat Flows) for structured
   outbound/inbound outreach. Tools mount per flow node; response tools are
   rejected until the shift-details audio has actually been delivered.
4. **In-app coordinator agent** — `build_general_sidebar_tools()`
   (`phoebe_event_agent/tools/__init__.py`): an always-on "hot set" (entity
   search/details, business metrics, memory writes, self-poke scheduling,
   drafting) plus feature-flag-gated toolbar tools (`has_create_shift`,
   `has_outreach_sending`, `has_chat_settings_editing`,
   `has_urgent_outreach`, agent-note routing) plus domain skills mounted
   dynamically via `load_skill`. Outreach-analysis runs bind to one
   outreach and get the outreach management/fill/monitoring set. Headless
   automations reuse the same tools with a different approval list.
   Subagent runs get a read-only tool set. High-consequence tools carry an
   **approval card**: the coordinator approves each call unless an
   always-allow grant exists (`agent_tool_approval_rules`, USER or
   CONVERSATION scope).
5. **Post-call write layer (voice)** — not live LLM tools:
   `services/worker/handlers/voice/qa_voice_agent_actions.py` (clock
   writebacks, reminder-response bookkeeping, contact-attempt outcome
   overwrites, availability-update deletion) and
   `after_hours_receptionist_generate.py` (receptionist items). Voice clock
   writebacks (#13586) and receptionist capture (PHO-14993 #13108) were
   both moved here from live tools recently.

---

## 2. Write tool inventory — SMS engine

Location: `libraries/python/sms_conversation_engine/tools/`.

| # | Tool | Parameters | What it does / writes | Gating |
|---|------|------------|----------------------|--------|
| 1 | `handle_callout_response_tool_v2` | `contact_attempt_id`, `shift_responses[]` (interested \| not_interested \| needs_review), `needs_review_owner?` (phoebe \| coordinator) | Publishes `CalloutsResponseHandleV2Payload`. Downstream handler updates `contact_attempts`, drives shift assignment / confirmations / coordinator handoff on `outreaches` + `shifts` | always on |
| 2 | `set_caregiver_availability` | `start_time`, `end_time` (ISO-8601) | INSERT `org_caregiver_availability` (AVAILABLE, `source=llm_tool`), replacing overlapping llm_tool rows | always on |
| 3 | `set_caregiver_unavailability` | `start_time`, `end_time`, `reason` (time_off_request \| shift_offer_decline) | INSERT `org_caregiver_availability` (UNAVAILABLE); rejects on conflict with scheduled shifts and publishes `CaregiverAvailabilityConflictNotifyPayload` | always on |
| 4 | `set_caregiver_weekly_availability` | `day_of_week`, `start_time`, `end_time` | INSERT `org_caregiver_weekly_availability` (AVAILABLE, `source=llm_tool`), replaces overlaps, splits overnight ranges | always on |
| 5 | `set_caregiver_weekly_unavailability` | same as #4 | Same table, UNAVAILABLE rows | always on |
| 6 | `handle_clock_in_tool` | `shift_id`, `clock_hour?`, `clock_minute?`, `clock_date?`, `timezone?` | Publishes `ShiftClockInExecutePayloadV1` (15 s delay); marks clock writeback pending (suppresses reminders); handler writes `shift_progressions` + EHR writeback | org `clock_in_writes_enabled` |
| 7 | `handle_clock_out_tool` | same as #6 | Publishes `ShiftClockOutExecutePayloadV1`; same downstream shape | org `clock_out_writes_enabled` |
| 8 | `handle_clock_in_reminder_response_tool` | — | UPDATE `contact_attempts.response_timestamp` on the active clock-in outreach | always on |
| 9 | `handle_clock_out_reminder_response_tool` | — | Same for clock-out outreach | always on |
| 10 | `handle_shift_confirmation_response_tool` | `shift_confirmation_id`, `response` (confirm \| decline \| unclear) | Publishes `ShiftConfirmationsResponseHandlePayload`; handler updates `shift_confirmations` / `shifts`, may cascade cancellation + client notification + EHR sync | shift-confirmation domain context |
| 11 | `handle_upcoming_shift_cancellation_tool` | `shift_id` | Two-step flow: first valid call returns `confirmation_required`; on confirmation records the cancellation honoring `shift_cancellation_transfer_threshold_hours` | snapshot gate |
| 12 | `prepare_shift_cancellation_selection_tool` | `shift_ids[]` (≥2) | Builds a numbered disambiguation prompt (conversation state only) | same as #11 |
| 13 | `save_memory` | `note` (≤2000 chars) | INSERT `org_caregiver_memories` (idempotent) + `org_caregiver_memory_fragments` (`source=SMS_ENGINE`); publishes `MemoryCompilePayload` | rejects unknown caregiver |
| 14 | `send_contact_card_tool` | — | Sends branded MMS vCard (idempotent); UPSERT `organization_onboarding_settings` | Twilio-backed phone only |
| 15 | `mark_conversation_complete_tool` | `reason` | No DB write; suppresses reply, marks conversation complete | always on |
| 16 | `record_caregiver_preference_answer` | `action`, `question_key?`, survey answer fields | Advances the caregiver scheduling-preference survey; persists answers + survey state | active survey conversations only (exclusive tool set) |
| 17 | `offer_open_shifts_tool` | `window_start_date?`, `window_end_date?` | Records a shift-discovery offer (creates outreach + offer payload) | `shift_discovery_enabled` — **0 uses in 30 d** |
| 18 | `record_intent_tool` | `intent` (clock_in \| clock_out) | Observability metric only | always on |

Read-only SMS tools: `get_caregiver_availability`,
`get_caregiver_weekly_availability`, `search_shifts_tool`,
`get_assigned_shift_details_tool`, `get_shift_details_tool`,
`get_care_plan_detail_tool`.

---

## 3. Write tool inventory — voice

### 3a. Core tools (all voice agents; `services/voice/tools/core.py`)

| Tool | Parameters | What it does | Notes |
|------|------------|--------------|-------|
| `end_call` | — | Terminal action; queues EndFrame, drains TTS, ends call | preflight guards (both parties spoke) |
| `transfer_call` | `destination_id?`, `reason?` | Queues Twilio transfer to a configured office destination with whisper | loop/self-transfer guards |
| `mark_as_voicemail` | — | Flags voicemail classification | excluded from inbound agents |

### 3b. Scenario tools by agent

| Tool | Parameters | What it writes | Mounted on |
|------|------------|----------------|-----------|
| `handle_callout_response` | `contact_attempt_id`, `shift_responses[]` | Deferred in Redis; flushed at call end as `CalloutsResponseHandleV2Payload` | outreach agent |
| `mark_declined_shifts_unavailable` | `shift_numbers[]` | `CaregiverAvailabilitySetPayload` per declined offered shift | outreach agent |
| `set_caregiver_availability` / `set_caregiver_unavailability` (voice) | `start_time`, `end_time` (+`reason`) | Publish availability payloads; conflict → notify + transfer | outreach (cascade), inbound |
| `handle_outreach_response` | `shift_responses[]`, `contact_attempt_id?` | Publishes `CalloutsResponseHandleV2Payload` immediately (Redis claim) | inbound pending-responses agent |
| `handle_shift_confirmation_response` | `response` (confirm \| decline), `shift_confirmation_id?` | Publishes `ShiftConfirmationsResponseHandlePayload`; decline near start auto-transfers | inbound agent |
| `handle_upcoming_shift_cancellation` | `shift_id` | Publishes cancellation payload, or transfer-to-office inside threshold | inbound agent |
| `record_caregiver_preference_answer_voice` | `action` + survey fields | POSTs survey transition to API | survey agent, inbound (active survey) |
| `record_shift_discovery_offer` / `record_shift_discovery_responses` / `express_shift_interest` | shift ids / responses | Publish shift-discovery payloads | gated `shift_discovery_enabled` — **0 uses in 30 d** |

Defined but NOT mounted on any live agent at this commit:

- `escalate_inbound_issue` — exported, never included. 0 uses.
- `handle_clock_in` / `handle_clock_out` (voice) — removed from live agents
  (#13586); clock reminder agents carry core tools only, post-call QA owns
  the writeback.
- `capture_receptionist_item` — removed (PHO-14993 #13108); post-call
  worker extracts receptionist items now.

### 3c. Voice flows layer (`services/voice/flows/`)

Real writes: `record_outreach_response` (+ alias
`record_presented_outreach_decline`), `record_outreach_decline` (fixed
not_interested), `record_outreach_conditional_response` (fixed
needs_review) → `CalloutsResponseHandleV2Payload`;
`record_offered_shift_unavailability` → `CaregiverAvailabilitySetPayload`;
`leave_voicemail` / `close_unavailable_mailbox` → voicemail classification;
plus core `transfer_call` / `end_call` / `mark_as_voicemail`.

Conversation-control (no domain write): `present_outreach`,
`present_pending_outreach`, `repeat_pending_outreach`, `present_care_plan`,
`repeat_care_plan`, `finish_call`, `continue_call`,
`complete_additional_help`, `defer_outreach`, `close_no_response`,
`close_wrong_recipient`, `repeat_*_final_check`.

The realtime speech-to-speech pipeline (`services/voice/realtime/tools.py`)
carries `start_closing`, `end_call`, `transfer_call`.

### 3d. Post-call write layer (voice)

- Clock writebacks from QA-verdict analysis
  (`ShiftClockIn/OutExecutePayloadV1`).
- Clock reminder response bookkeeping (`contact_attempts.response_timestamp`).
- Contact-attempt outcome overwrites; deletion of wrong llm_tool
  availability updates.
- After-hours receptionist item generation.
- Post-call caregiver/client memory generation.

---

## 4. Write tool inventory — in-app coordinator agent

Location: `libraries/python/phoebe_event_agent/tools/` (Slack-bound tools in
`libraries/python/phoebe_slack/`). "Approval" = shows a per-call approval
card unless an always-allow grant exists. The interactive approval list is
`GENERAL_SIDEBAR_APPROVAL_TOOL_NAMES` (`runtime_assembly.py:401`); headless
automations use `AUTOMATION_APPROVAL_TOOL_NAMES`, which drops
`draft_agent_outreach` so automations can create outreaches autonomously.

### 4a. Outreach lifecycle & contacting

| Tool | Parameters | What it writes | Approval / gating |
|------|------------|----------------|-------------------|
| `draft_agent_outreach` (`scheduling.py:330`) | `shift_ids[]`, `suggested_caregiver_ids?`, `outreach_type`, urgency, `callout_reason?`, `instructions?`, recurrence, `auto_start?`, `auto_monitor?`, `start_at?`, `shift_groups?`, auto-assign params, `shift_care_plans?` | INSERT `outreaches` (+ shift links, default subscribers), org events, queues outreach-analysis agent kickoff | approval (interactive only); urgency gated `has_urgent_outreach` |
| `manage_outreach_start_with_contacts` (`outreach_management.py:785`) | `outreach_id`, caregiver→shift map, `acknowledged_rejections?`, `acknowledged_conflicts?` | `outreaches.status` → IN_PROGRESS, INSERT `contact_attempts` + per-shift response rows, pokes callout orchestration | approval; conflict/rejection acknowledgment required |
| `manage_outreach_add_contact_attempts` (`outreach_management.py:1011`) | `outreach_id`, `caregiver_ids[]`, `shift_ids[]`, `delivery_mode` (sms \| voice), acknowledgments | INSERT `contact_attempts` (round-0 or ad hoc retry), pokes workflow | approval |
| `add_outreach_suggestions` (`outreach_management.py:329`) | `outreach_id`, `suggestions[]` (caregiver + covered shifts), `mode` (append \| overwrite), `acknowledged_rejections?` | UPDATE `outreaches.suggested_caregivers` (with row lock) | capability approval; fork-rejection checks |
| `manage_outreach_remove_suggestions` (`outreach_management.py:486`) | `outreach_id`, `caregiver_ids[]` | UPDATE `outreaches.suggested_caregivers` | bound-outreach validation |
| `manage_outreach_set_status` (`outreach_management.py:586`) | `outreach_id`, `action` (pause \| resume \| cancel) | UPDATE `outreaches.status`, cancels workflows, org events | lifecycle transition validation |
| `update_agent_outreach` (`outreach_management.py:1280`) | `outreach_id`, `shift_ids?`, `shift_groups?`, auto-assign config, `callout_reason?`, recurrence, `shift_care_plans?` | UPDATE `outreaches.shift_ids` + metadata, syncs `outreach_shifts`, org events, kickoff messages | shift/group edits PENDING-only |
| `update_outreach_metadata` (`outreach_metadata.py:50`) | `outreach_id`, `instructions?`, `opening_line?`, 6 SMS template fields, `notify_client_on_fill?` | UPDATE `outreaches.outreach_metadata` + steps, pokes workflow | approval |
| `fill_callout_shifts` (`outreach_fill.py:227`) | `callout_id`, `shift_ids[]`, `caregiver_id`, `notify_non_responders?`, `propagate_to_ehr` (default true), rate params, `assign_without_response?` | Assigns shifts to caregiver: shift status + assignment, EHR sync messages, response entries | approval; gated `has_outreach_sending`; rate/takeover validation |
| `cancel_callout_shifts` (`shift_management.py:1851`) | `outreach_id`, `shift_ids[]`, `removal_reason` (allowlist), `notify_caregivers_reason?`, `include_non_responders?` | Detaches shifts from `outreaches.shift_ids`, cancelled-shifts audit trail, notifications, EHR sync, activity events | active-status outreach only |
| `notify_caregivers` (`outreach_management.py:678`) | `outreach_id`, `notification_reason`, `shift_ids?`, `include_non_responders?`, custom messages | Starts ShiftHandledNotifyWorkflow (SMS to contacted caregivers); no status change | approval |
| `notify` (`outreach_compat.py:57`) | `outreach_id`, `title`, `message`, `level?` | In-app notification to outreach subscribers | bound outreach |
| `monitor_outreach` (`outreach_monitoring.py:132`) | `outreach_id`, `reason`, `conditions?`, `pre_approved_actions?` | Monitoring schedule records (re-check on new responses until latest shift end) | active/pending/paused outreach |
| `stop_agent_monitoring` (`outreach_monitoring.py:271`) | `outreach_id`, `reason` | Terminates monitoring schedule | — |
| `override_contact_attempt_response` (`outreach_review.py:85`) | `contact_attempt_id`, `shift_id`, `response` (nullable) | UPSERT the (attempt, shift) response row; null undoes | — |
| `submit_outreach_suggestion_feedback` (`outreach_review.py:315`) | `outreach_id`, `caregiver_id`, sentiment, `reason?` | `outreaches.suggested_caregivers_feedback` map (ranking signal) | — |
| `add_outreach_subscriber` / `remove_outreach_subscriber` (`outreach_review.py:763/847`) | `outreach_id`, user | Subscriber list rows | 0 uses in 30 d |
| `update_callout_excluded_caregivers` (`outreach_settings.py:390`) | add/remove caregiver ids | Durable callout do-not-contact list | — |
| `update_outreach_filter_rule` (`outreach_filter_rules.py:235`) | rule fields | INSERT/DELETE `outreach_filter_rules` (SKIP/ALLOW entries) | — |

### 4b. Shifts & scheduling

| Tool | Parameters | What it writes | Approval / gating |
|------|------------|----------------|-------------------|
| `create_shift` (`shift_management.py:231`) | patient identity/address, start/end times | INSERT `shifts` (NEEDS_COVERAGE) | approval; gated `has_create_shift` — **0 uses in 30 d** |
| `update_shift` (`shift_management.py:401`) | `shift_id`, identity/address/times/notes fields, `unassign_caregiver?`, writeback scope | UPDATE `shifts`, assignment state, EHR sync messages, activity events | time/writeback validation |
| `cancel_shift` (`shift_management.py:1166`) | `shift_id`, `notification_reason?` | EHR-first cancel (nothing changes if EHR write fails), then `shifts.status` → CANCELLED, cancels pending clock reminders | per-call coordinator approval semantics in description |
| `update_shift_triage_stage` (`shift_management.py:1578`) | `shift_id`, `stage` (ShiftStage minus CLOCKED_IN/OUT) | Appends to shift progression log | rate-limited 3/min/shift |
| `add_shift_note_to_ehr` (`shift_management.py:828`; alias `add_shift_note_to_wellsky`) | `shift_id`, note text, `artifact_kind` | Writes note to source EHR (AxisCare memo / WellSky note) + Phoebe record, writeback queue | shift-memo writeback flag |
| `assign_shifts_to_caregiver` (`schedule.py:1330`) | `shift_ids[]`, `caregiver_id`, `allow_reassignment?`, rate params | Shift assignment + EHR sync + response records | approval |
| `schedule_self_poke` (`scheduling.py:1737`) | `delay_minutes` (15–10080), `reason`, `instructions`, `pre_approved_actions?` | Temporal self-poke workflow + pending event rows (one-shot wake-ups) | pending-poke cap, cost ceiling |
| `trigger_ehr_sync` (`data_sync.py:50`) | `client_id?` / `caregiver_id?` | Queues on-demand EHR → Phoebe sync | EHR integration enabled |

### 4c. Records, memory, messaging, settings

| Tool | Parameters | What it writes | Approval / gating |
|------|------------|----------------|-------------------|
| `update_caregiver_record` (`caregiver_record.py`) | `caregiver_id`, `agent_instructions?`, languages, `roles?`, `employment_type?`, `desired_weekly_hours?` | UPDATE `org_caregiver_records` (no identity/demographic fields) | — |
| `update_client_record` (`client_record.py:83`) | `client_id`, `default_notify_on_fill?`, `care_plan_summary?` | UPDATE `org_client_records` | — |
| `update_client_phone_preference` (`client_record.py:257`) | `client_id`, `mobile` \| `home` | Client contact preference | 0 uses in 30 d |
| `assign_client_care_coordinator` / `remove_client_care_coordinator` (`client_record.py:391/479`) | `client_id`, coordinator | Care-coordination assignment rows | 0 uses in 30 d |
| `save_caregiver_memory_instruction` (`caregiver_memory.py:68`) | `caregiver_id`, instruction | `org_caregiver_memory_fragments` + compile queue (same path as the memory tab) | approval |
| `save_client_memory_instruction` (`client_memory.py:68`) | `client_id`, instruction | Client memory fragments + compile queue | approval |
| `save_note` (`agent_notes.py`) | note text, domain routing | `agent_notes` rows (domain-routed) | `use_agent_note_memory_tools` rollout flag |
| `save_automation_memory` / `update_automation_memory` / `discard_automation_memory` (`automations.py:224/312/409`) | memory text, `memory_id` + version | `agent_automation_memories` (versioned, stale-write-rejected) | automation runs only |
| `add_caregiver_weekly_availability` (`caregiver_availability.py:63`) | caregiver, minute-of-week window, status | INSERT weekly availability; splits overlapping manual rows; rejects sync-managed overlaps | — |
| `add_caregiver_one_off_availability` (`caregiver_availability.py:187`) | caregiver, ISO window, status | INSERT one-off availability | — |
| `delete_caregiver_availability` (`caregiver_availability.py:289`) | `availability_id`, `recurrence` | DELETE (MANUAL-source rows only; sync-managed rejected) | — |
| `send_one_off_sms_in_conversation` (`conversation_messaging.py:754`) | `conversation_id` xor `caregiver_id`, `body`, `send_anyway?`, `language_override?` | Sends outbound SMS via canonical send path (HUMAN-sourced message); starts conversation if none | takeover/block checks |
| `place_one_off_voice_call_in_conversation` (`conversation_messaging.py:1052`) | `conversation_id` xor `caregiver_id`, call purpose | Enqueues one-off conversational voice call | takeover checks |
| `approve_staged_message` (`conversation_messaging.py:471`) | staged message id, `updated_content?` | Staged message → APPROVED + SMS dispatch (idempotent) | 0 uses in 30 d |
| `reject_staged_message` (`conversation_messaging.py:537`) | staged message id | Staged message → rejected, never sent | — |
| `edit_staged_message_content` (`conversation_messaging.py:644`) | staged message id, content | Edits pending draft | 0 uses in 30 d |
| `update_organization_general_settings` (`organization_general_settings.py:376`) | allowlisted boolean settings | UPDATE org general settings | approval; narrow allowlist |
| `surface_organization_setting` (`organization_general_settings.py:293`) | `setting_key` | Renders live editable settings card in chat (user applies the change) | gated `has_chat_settings_editing` |
| `update_callout_settings` (`outreach_settings.py:259`) | exposed callout settings fields | UPDATE callout settings | approval — 0 uses in 30 d |
| `set_tag_matching_importance` | tag weighting | Tag-match config | approval — 0 uses in 30 d |
| `update_recommendation_defaults` (`recommendation_defaults.py:210`) | org-wide recommendation limits | UPDATE recommendation defaults | — |
| `update_playbook_item_tool` / `apply_playbook_natural_language_edit` (`playbook_rules.py`) | playbook item id, edits / NL instruction | UPDATE `agent_playbook_items` | — |
| `resolve_activity_event` (`activity_feed.py:195`) | `activity_event_id` | Marks action-required activity event resolved | — |
| `submit_general_agent_feedback` (`general_agent_feedback.py:47`) | feedback text | Internal feedback record to Phoebe team | intentionally no approval card |
| `generate_report` (`generate_report.py:64`) | `report_id` or table data | Renders downloadable report artifact (run artifact, not org data) | — |
| `post_to_slack_thread` / `send_sms_to_caregiver` / `schedule_followup` (`phoebe_slack/`) | text / caregiver + body / delay | Slack post; SMS in Phoebe's voice; delayed wake-up | Slack-bound agent runs |

Removed on main but present in the 30-day traces: `update_execution_plan`
(run-plan tool, removed #12989; 25,902 calls in window).

Read/control tools (names, for the matrix): `get_recommendations`,
`load_skill`, `callouts_details`, `caregivers_search`/`_details`,
`shifts_search`/`_details`, `clients_search`, `client_details`,
`outreach_search`, `get_activity_feed`, `schedule_summary`,
`query_business_metrics`, `search_conversation_history`,
`search_voice_call_transcripts`, `search_notes`,
`search_caregiver_memories`, `list_playbooks`, `get_reliability_policy`,
`get_settings`, `get_filter_options`, `get_recommendation_defaults`,
`organization_scheduling_context`, `recommend_caregivers_for_shifts`,
`recommend_shifts_for_caregivers`, `list_caregiver_client_preferences`,
`product docs tools`, `get_current_datetime`, `ask_user_question` /
`ask_coordinator` (interaction), `preview_one_off_sms_translation`,
`query`, `view_skills`. Subagent runs receive only read tools.

---

## 5. Production usage — trailing 30 days

### 5a. SMS engine (function_call count; domain from `sms_engine_runs.domain_type`)

| Tool | Calls | Dominant use case |
|------|------:|-------------------|
| `handle_shift_confirmation_response_tool` | 71,398 | 99.8% shift confirmation |
| `handle_callout_response_tool_v2` | 53,004 | 98% callout |
| `set_caregiver_weekly_availability` | 17,910 | 92% general conversation |
| `set_caregiver_unavailability` | 17,410 | 88% callout (declines) |
| `mark_conversation_complete_tool` | 14,367 | callout 64%, general 22% |
| `search_shifts_tool` (read) | 7,261 | shifts/clock 61%, callout 27% |
| `record_intent_tool` | 6,978 | shifts/clock 63% |
| `save_memory` | 6,895 | general 76%, callout 22% |
| `set_caregiver_weekly_unavailability` | 4,330 | general 68% |
| `handle_clock_out_tool` | 2,333 | 99% shifts (clock) |
| `handle_clock_in_reminder_response_tool` | 2,314 | callout-typed reminders |
| `handle_clock_in_tool` | 2,305 | 98% shifts (clock) |
| `set_caregiver_availability` | 2,178 | 91% general |
| `handle_clock_out_reminder_response_tool` | 2,034 | callout-typed reminders |
| `record_caregiver_preference_answer` | 1,931 | survey conversations |
| `get_assigned_shift_details_tool` (read) | 561 | callout + confirmation |
| `send_contact_card_tool` | 271 | callout onboarding |
| `handle_upcoming_shift_cancellation_tool` | 211 | 91% shifts |
| `prepare_shift_cancellation_selection_tool` | 89 | general/shifts |
| other reads | ≤12 each | — |
| `offer_open_shifts_tool` | **0** | shift discovery gated off |

### 5b. Voice (tool_start count; direction / outreach type)

| Tool | Calls | Dominant use case |
|------|------:|-------------------|
| `end_call` | 330,874 | all call types |
| `mark_as_voicemail` | 197,705 | outbound outreach hitting voicemail |
| `transfer_call` | 40,808 | inbound + outreach handoffs |
| `handle_callout_response` | 22,999 | outbound callout 90%, recurring-shift 8% |
| `mark_declined_shifts_unavailable` | 5,634 | outbound declines |
| `capture_receptionist_item` | 3,367 | inbound after-hours — **removed; now post-call** |
| `handle_outreach_response` | 1,559 | inbound callbacks |
| `record_caregiver_preference_answer_voice` | 899 | outbound survey 79% |
| `handle_clock_in` / `handle_clock_out` | 586 / 492 | **removed; now post-call QA** |
| `handle_shift_confirmation_response` | 524 | inbound |
| `present_pending_outreach` (flow) | 515 | inbound flow |
| `set_caregiver_availability` / `set_caregiver_unavailability` | 232 / 149 | inbound + callout |
| `record_outreach_decline` (flow) | 209 | inbound flow |
| `finish_call` (flow) | 179 | inbound flow |
| `handle_upcoming_shift_cancellation` | 176 | inbound |
| `present_care_plan` / `repeat_care_plan` (flow) | 149 / 11 | inbound flow |
| clock reminder response voice tools | 118 / 77 | **removed; now post-call** |
| other flow-control tools | ≤105 each | inbound flow |
| shift discovery trio / `escalate_inbound_issue` | **0** | gated off / unmounted |

### 5c. In-app coordinator agent (function_call count; general vs outreach_analysis)

170,646 tool calls across 24,502 live conversations. 81% happen in
outreach-analysis (outreach-bound) runs.

| Tool | Calls | Notes |
|------|------:|-------|
| `get_recommendations` (read) | 43,651 | recommendation engine |
| `update_execution_plan` | 25,902 | **removed on main (#12989)** — run-plan control |
| `load_skill` (control) | 16,649 | dynamic skill mounting |
| `add_outreach_suggestions` | 12,557 | 99% outreach-analysis |
| `callouts_details` (read) | 7,545 | |
| `manage_outreach_add_contact_attempts` | 7,200 | 98% outreach-analysis |
| `caregivers_search` (read) | 6,497 | |
| `shifts_search` / `shifts_details` (read) | 4,422 / 3,118 | |
| `send_one_off_sms_in_conversation` | 4,389 | both run types |
| `draft_agent_outreach` | 3,965 | 94% general sidebar |
| `monitor_outreach` | 2,587 | outreach-analysis |
| `manage_outreach_remove_suggestions` | 2,370 | |
| `review_outreach` (read) | 2,284 | |
| `schedule_self_poke` | 2,008 | |
| `manage_outreach_start_with_contacts` | 1,928 | |
| `ask_user_question` (control) | 1,860 | |
| `notify` | 1,604 | in-app notifications |
| `get_caregiver_recommendation_details` (read) | 1,559 | |
| `clients_search` (read) | 1,347 | |
| `save_caregiver_memory_instruction` | 1,218 | |
| `manage_outreach_set_status` | 1,120 | |
| `get_activity_feed` (read) | 1,061 | |
| `update_outreach_metadata` | 964 | |
| `update_shift` | 852 | |
| `place_one_off_voice_call_in_conversation` | 804 | |
| `update_agent_outreach` | 620 | |
| `fill_callout_shifts` | 541 | |
| `save_client_memory_instruction` | 539 | |
| `post_to_slack_thread` | 521 | Slack-bound runs |
| `stop_agent_monitoring` | 435 | |
| `trigger_ehr_sync` | 422 | |
| `notify_caregivers` | 403 | |
| `update_outreach_filter_rule` | 328 | |
| `resolve_activity_event` | 294 | |
| `cancel_callout_shifts` | 257 | |
| `override_contact_attempt_response` | 225 | |
| `apply_playbook_natural_language_edit` | 217 | |
| `assign_shifts_to_caregiver` | 127 | |
| `add_caregiver_weekly_availability` / `add_caregiver_one_off_availability` / `delete_caregiver_availability` | 98 / 89 / 7 | |
| `generate_report` | 48 | |
| `submit_general_agent_feedback` | 39 | |
| `update_playbook_item_tool` | 32 | |
| `schedule_followup` | 29 | Slack-bound |
| `cancel_shift` | 12 | |
| `update_shift_triage_stage` / `add_shift_note_to_ehr`(+wellsky alias) | 7 / 10 | |
| `update_recommendation_defaults` | 6 | |
| `reject_staged_message` | 6 | |
| `update_caregiver_record` / `update_client_record` | 4 / 3 | |
| `update_callout_excluded_caregivers` | 2 | |
| `update_organization_general_settings` / `submit_outreach_suggestion_feedback` / `send_sms_to_caregiver` / `save_note` / `save_automation_memory` | 1 each | |
| `create_shift`, `approve_staged_message`, `edit_staged_message_content`, `update_client_phone_preference`, care-coordinator tools, subscriber tools, `update_callout_settings`, `set_tag_matching_importance` | **0** | defined, unused in window |

---

## 6. Capability matrix

### 6a. Caregiver/client-facing (SMS + voice)

| Capability | SMS | Voice (live) | Post-call | 30 d volume |
|------------|-----|--------------|-----------|-------------|
| Respond to callout / open-shift offer | yes | yes (agent + flow) | — | 53,004 + 24,907 |
| Confirm / decline shift confirmation | yes | yes (inbound) | — | 71,398 + 524 |
| Request cancellation of assigned shift (policy-gated) | yes | yes (inbound) | — | 300 + 176 |
| Record one-off availability / unavailability | yes (direct DB) | yes (queue) | QA can delete wrong ones | 19,588 + 381 |
| Record weekly recurring availability | yes | no | — | 22,240 |
| Mark declined offered shifts unavailable | via reason field | dedicated tool | — | + 5,634 |
| Clock in / out (EHR writeback) | yes (org-gated) | no — moved post-call | yes | 4,638 SMS |
| Record clock reminder response | yes | no — moved post-call | yes | 4,348 SMS |
| Save caregiver memory | yes | no live tool | yes (post-call generation) | 6,895 |
| Advance preference survey | yes | yes | — | 1,931 + 899 |
| Shift discovery offers | gated off | gated off | — | 0 |
| Send org contact card (MMS) | yes | n/a | — | 271 |
| End conversation / call | yes | yes | — | 14,367 + 330,874 |
| Transfer / escalate to human | no direct tool (needs_review_owner routing) | `transfer_call` | — | 40,808 voice |
| Voicemail handling | n/a | yes | — | 197,705 |
| After-hours receptionist message capture | no | no — moved post-call | yes | 3,367 (pre-removal) |
| Present care plan | read tool (rarely enabled) | inbound flow | — | 6 + 160 |

### 6b. Coordinator-facing (in-app agent) — what v3 must reach parity with

| Capability | Tools | 30 d writes | Approval-gated |
|------------|-------|------------:|----------------|
| Curate outreach suggestions | add/remove suggestions | 14,927 | partly |
| Queue caregiver contact attempts (SMS/voice) | manage_outreach_add_contact_attempts, start_with_contacts | 9,128 | yes |
| Draft new outreaches | draft_agent_outreach | 3,965 | yes (interactive) |
| Direct 1:1 messaging (SMS + voice call) | send_one_off_sms / place_one_off_voice_call (+ staged-message controls) | 5,199 | no |
| Outreach lifecycle control | set_status, update_agent_outreach, update_outreach_metadata | 2,704 | partly |
| Self-scheduling / monitoring (agentic loop) | schedule_self_poke, monitor_outreach, stop_agent_monitoring | 5,030 | no |
| Fill / assign shifts to caregivers | fill_callout_shifts, assign_shifts_to_caregiver | 668 | yes |
| Shift CRUD + EHR notes | update/cancel/create shift, triage stage, EHR notes | ~880 | partly |
| Caregiver/client memory + records | memory instructions, record updates, availability CRUD | ~1,960 | memory: yes |
| Coordinator notifications | notify, notify_caregivers | 2,007 | notify_caregivers: yes |
| Org configuration | filter rules, callout settings, general settings, recommendation defaults, playbooks | ~590 | settings: yes |
| Response corrections | override_contact_attempt_response | 225 | no |
| EHR sync trigger | trigger_ehr_sync | 422 | no |
| Activity feed triage | resolve_activity_event | 294 | no |
| Reporting | generate_report + read/report tools | 48 | no |
| Slack surface | post_to_slack_thread, send_sms_to_caregiver, schedule_followup | 551 | send_sms: yes |

## 7. Observations for the v3 gap analysis

1. Caregiver-facing writes concentrate in two paths: shift confirmation
   responses (71k/mo) and callout responses (76k/mo across channels).
   Coordinator-facing writes concentrate in outreach
   curation/contacting (~24k/mo) and direct messaging (~5k/mo).
2. Availability is the third caregiver pillar (~47k writes/mo) and the only
   domain where SMS writes tables directly rather than publishing queue
   messages.
3. Nearly every other write funnels through queue payloads
   (`CalloutsResponseHandleV2Payload`,
   `ShiftConfirmationsResponseHandlePayload`,
   `ShiftClockIn/OutExecutePayloadV1`, `CaregiverAvailabilitySetPayload`,
   `MemoryCompilePayload`) or through shared service paths (canonical SMS
   send, EHR writeback queues). v3 can reuse these seams wholesale.
4. The in-app agent has a mature approval-card system
   (`agent_tool_approval_rules`, per-user/per-conversation always-allow,
   separate headless list for automations). v3 needs an equivalent before
   it can carry the high-consequence tools (contacting, filling,
   assigning, settings).
5. The recent architectural direction on voice is to REMOVE live write
   tools and decide post-call (clock writebacks #13586, receptionist
   capture #13108); the in-app agent similarly dropped its
   `update_execution_plan` run-plan tool (#12989). Trace counts for these
   removed tools are pre-removal.
6. Dormant surface (0 uses): shift discovery (both caregiver channels),
   `escalate_inbound_issue` (never mounted), `create_shift`
   (feature-gated), staged-message approve/edit, client care-coordinator
   and phone-preference tools, callout settings / tag-importance tools.
   Check intent before planning v3 parity against these.
7. SMS has no human-transfer tool; escalation rides inside
   `handle_callout_response_tool_v2` (`needs_review_owner`). Voice and
   in-app have explicit transfer/escalation. If v3 unifies channels,
   transfer semantics need one design.
