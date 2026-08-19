---
type: reference
tags: [phoebe, agent, v3, write-tools, design]
created: 2026-08-18
updated: 2026-08-18
---

# v3 write-tools consolidation design

## Problem statement

The audit counts 90 customer-facing write tools: 17 SMS, 14 voice, and 59
in-app chat tools. It classifies 34 as high risk, 44 as medium risk, and 12
as low risk (`agent-write-tools-audit-epic.md:44-55`). The five highest-risk
tools start live contact, assign shifts, cancel work, or send SMS
(`agent-write-tools-audit-epic.md:57-67`).

This audit does not describe the current v3 registry. It explicitly excludes
`phoebe_v3_agent` (`agent-write-tools-audit-epic.md:22-35`). Current v3 has no
customer-facing business write tool. Its registry exports bash, interaction,
poke, and query groups (`libraries/python/phoebe_v3_agent/tools/__init__.py:1-17`).
The bundle mounts five callable tools: `run_bash`, `query`,
`show_dynamic_ui`, `schedule_poke`, and `cancel_poke`
(`libraries/python/phoebe_v3_agent/sidebar/bundle.py:103-126`).

The proposed `write` tool is therefore a new v3 door. The audit paths below
are the legacy implementations that the new registry must adapt. There is no
existing v3 code path for these write names. This discrepancy matters because
the design must preserve downstream worker effects, not copy legacy function
names into v3.

The current shape creates five kinds of sprawl:

- availability has four SMS tools for dated and weekly available and
  unavailable intervals. They use the same overlap-replacement domain helper
  (`sms_conversation_engine/tools/availability_tools.py:239,359,704,912`).
  In-app chat adds three more names for add, add-one-off, and delete
  (`phoebe_event_agent/tools/caregiver_availability.py:62,186,288`).
- outreach contact work splits one lifecycle across
  `manage_outreach_start_with_contacts` and
  `manage_outreach_add_contact_attempts`. Both create contact attempts and
  poke the same workflow (`phoebe_event_agent/tools/outreach_management.py:702,930`).
- staged messaging has separate approve, reject, and edit tools, plus separate
  one-off SMS and voice tools. All mutate or send conversation state
  (`phoebe_event_agent/tools/conversation_messaging.py:185,251,358,468,614`).
- shift lifecycle work has separate create, update, cancel, assign, triage,
  and EHR-note tools. They all write shift or progression state and share
  assignment and writeback safety checks
  (`phoebe_event_agent/tools/shift_management.py:233,403,831,1165,1577`,
  `phoebe_event_agent/tools/schedule.py:1251`).
- settings use specialized tools for general replies, callout behavior,
  exclusions, recommendation defaults, and tag matching. The audit notes that
  the source called this generic `write_settings`, while current code has
  several names (`agent-write-tools-audit-epic.md:1080-1144`).

The same business action appears under SMS, voice, and chat names. Some tools
write directly. Others enqueue a payload and let a worker write later. The
audit warns that counting only `session.add` misses those downstream effects
(`agent-write-tools-audit-epic.md:65-67`). v3 must expose one stable contract
for both paths.

The prior v3 harness design already selects one write door:
`write(entity, operation, payload | file)`. It also requires policy to hang
off entity and operation, not tool names. This design keeps that direction and
uses a discriminated operation registry behind one callable tool.

## Design principles

These principles apply to the v3 registry and its generated tool description.
They follow `PROMPTING.md`, which requires minimal high-signal context, one
home per rule, just-in-time data, and generated schemas near enforcement
(`libraries/python/phoebe_v3_agent/PROMPTING.md:20-121`).

1. **Use one write door.** The model sees `write`, then selects an entity and
   operation. Authorization, validation, confirmation policy, idempotency, and
   downstream dispatch live in the entity-operation registry. An anti-example
   is `update_organization_general_settings` beside
   `update_callout_settings`; those are separate names for organization
   policy writes (`phoebe_event_agent/tools/organization_general_settings.py:375`,
   `outreach_settings.py:258`).

2. **Put parameters before tool names.** Use `operation` or `mode` when the
   service and safety boundary are shared. `create`, `update`, and `discard`
   for automation memory become one `memory` entity with one operation union.
   The anti-example is `save_automation_memory`, `update_automation_memory`,
   and `discard_automation_memory` (`phoebe_event_agent/tools/automations.py:223,311,408`).

3. **Use resource-verb pairs inside the registry.** Each registry row has a
   logical name such as `shift.create`, `shift.update`, or `outreach.send`.
   The callable surface stays `write` to keep one discovery and policy door.
   Do not invent names such as `fill_callout_shifts` when the operation is a
   shift assignment. That name hides the resource and duplicates
   `assign_shifts_to_caregiver` (`phoebe_event_agent/tools/outreach_fill.py:228`,
   `schedule.py:1251`).

4. **Use discriminated payloads.** The `entity` and `operation` pair selects an
   exact payload schema. This keeps one tool without accepting an untyped
   dictionary. The anti-example is the survey family, where SMS and voice
   carry the same `action` and `question_key` shape under different tool names
   (`sms_conversation_engine/tools/caregiver_preference_tools.py:65`,
   `voice/tools/caregiver_preference_survey.py:115`).

5. **Make draft and send parameters.** `execution: "draft" | "apply"` and
   `delivery: "draft" | "send"` belong in the operation payload. A second
   tool is not a confirmation model. `show_dynamic_ui` already treats a
   `message_preview` as staged and not sent
   (`phoebe_v3_agent/tools/interaction/tool.py:24-35,164-170`). The anti-example
   is the staged-message trio, where approve, reject, and edit are separate
   tools instead of one message state machine.

6. **Fold lookup context into the write request.** A write may accept a stable
   entity ID or a bounded reference selector. It must not require a separate
   lookup tool when the lookup only exists to feed the write. `query` remains
   a general read tool, but write adapters should accept `shift_id`,
   `caregiver_id`, and similar IDs directly. The current `query` tool is
   read-only SELECT execution (`phoebe_v3_agent/tools/query/tool.py:57-75`).
   The anti-example is a future design that adds one lookup tool per write
   family merely to resolve an ID.

7. **Keep side-effect policy outside prompt prose.** The registry owns risk,
   approval, feature gates, mode checks, idempotency, and worker dispatch. The
   prompt describes how to choose the operation. It does not restate every
   gate. This follows the v3 rule that numbers and enforced schemas live near
   code (`PROMPTING.md:104-119`). The anti-example is relying on the current
   bundle's `tool_approval_config=None` as a permanent write policy
   (`phoebe_v3_agent/sidebar/bundle.py:138-145`).

### Costs of consolidation

One write door loses the immediate type signal that a narrow name gives the
model. A model can choose an invalid entity-operation pair unless the schema
and refusal are strict. The registry must therefore generate a JSON Schema
`oneOf`, validate it before dispatch, and return repair-grade errors.

One write door also makes tool analytics less direct. Metrics must group by
`entity`, `operation`, `surface`, and `risk`, not only by `tool_name`.

Finally, a shared adapter can hide real semantic differences. The registry
must keep separate rows when authorization, transaction order, or external
side effects differ. Consolidation changes the model-facing name. It does not
merge the underlying service methods or their safety checks.

## Proposed consolidation patterns

### 1. one-write-door

**When it applies:** Use this for customer-facing mutations that need the same
organization scope, audit trail, approval wait, and idempotency contract.

**Canonical shape:**

```json
{
  "entity": "shift",
  "operation": "assign",
  "execution": "apply",
  "idempotency_key": "stable-call-key",
  "input": {"payload": {"shift_ids": ["..."], "caregiver_id": "..."}}
}
```

**Worked example:** Convert all 90 audited customer-facing write names into
one `write` tool. The registry still exposes 24 logical entity-operation rows.
The model sees one callable name, while policy and validation see the precise
row.

### 2. collapse-by-verb

**When it applies:** Use this when old tools call one domain service with
different CRUD verbs or state transitions.

**Canonical shape:** `entity=memory`, `operation=create|update|discard`, with
an operation-specific payload.

**Worked example:** Convert `save_automation_memory`,
`update_automation_memory`, and `discard_automation_memory` into
`write(entity="memory", operation="update", payload={"subject_type":"automation", ...})`.

**Cost:** A single tool can expose a destructive discard operation beside a
create operation. The registry must mark risk per operation and validate
version fields for update and discard.

### 3. availability-state-union

**When it applies:** Use this when the old tools differ only by available or
unavailable state, one-off or weekly scope, or add versus replace behavior.

**Canonical shape:** `entity=caregiver_availability`,
`operation=update`, with `scope`, `state`, and `mode` fields.

**Worked example:** Convert the four SMS availability tools, the two voice
availability actions, `mark_declined_shifts_unavailable`, and the three chat
availability tools into one operation.

**Cost:** The old names prevented invalid combinations by construction. The
new validator must reject missing weekly fields, one-off fields in weekly
mode, and `state` on a remove operation.

### 4. discriminated-payload

**When it applies:** Use this when one service has related actions with
different required fields.

**Canonical shape:** `entity=shift`, `operation=mutate`, with a payload union
selected by `action=create|update|cancel|assign|triage|add_ehr_note`.

**Worked example:** Convert `create_shift`, `update_shift`, `cancel_shift`,
`assign_shifts_to_caregiver`, `update_shift_triage_stage`, and
`add_shift_note_to_ehr` into one `shift` operation family under `write`.

**Cost:** The payload is larger than any one old schema. Generated `oneOf`
schemas and branch-specific errors are required.

### 5. draft-as-flag

**When it applies:** Use this when a draft is the same resource in a pending
state, and apply or reject uses the same ownership and authorization checks.

**Canonical shape:** `entity=communication`, `operation=send`,
`delivery=draft|send`, with `staged_message_id` required for an existing draft.

**Worked example:** Convert `approve_staged_message`,
`reject_staged_message`, `edit_staged_message_content`,
`send_one_off_sms_in_conversation`, and
`place_one_off_voice_call_in_conversation` into communication operations.

**Cost:** Persistent staged messages have a lifecycle that one-off messages
do not. The payload must require a staged ID for edit, send-existing, and
reject. It must reject staged-only fields for a new one-off send.

### 6. surface-adapter

**When it applies:** Use this when SMS, voice, and in-app tools enqueue the
same domain payload but resolve IDs from different context objects.

**Canonical shape:** The v3 payload always uses explicit IDs. Legacy adapters
resolve conversation or call context once, then call the same registry row.

**Worked example:** Convert `handle_callout_response_tool_v2`,
`handle_callout_response`, and `handle_outreach_response` into
`shift_response.resolve` with explicit attempt and shift IDs.

**Cost:** Context-only voice tools no longer look simple. Voice adapters must
fail when a pending ID cannot be resolved. They must not silently pick the
first pending response.

### 7. check-in-state-machine

**When it applies:** Use this for schedule and cancel operations that share a
conversation budget and durable pending-event model.

**Canonical shape:** `conversation.check_in` with `action=schedule|cancel`.

**Worked example:** Convert current v3 `schedule_poke` and `cancel_poke`, plus
legacy `schedule_self_poke`, into one tool. The existing detailed scheduling
rules remain in the tool description and scheduler, not in the base prompt.

**Cost:** Schedule and cancel have different required fields. The schema must
use a strict action union. This is still safer than presenting two names for
one pending-event resource.

## Post-consolidation tool list

The v3 callable write surface has two tools. `write` owns the 90 audited
business and control side effects. `conversation.check_in` owns scheduled
wakes. The existing read and interaction tools remain `run_bash`, `query`, and
`show_dynamic_ui`; they are not write tools. The final callable registry has
five names.

### `write`

**Purpose:** Apply one validated organization-scoped mutation or side effect
through the entity-operation registry.

**Top-level argument schema:**

```text
write = {
  entity: enum[
    "caregiver_availability", "caregiver_preference", "memory", "note",
    "person", "shift", "shift_response", "clock", "communication",
    "outreach", "organization_policy", "playbook", "relationship",
    "sync", "feedback", "report", "conversation"
  ], required,
  operation: enum[
    "update", "create", "discard", "resolve", "mutate", "send",
    "complete", "transfer", "classify", "monitor", "assign", "remove",
    "trigger", "record_intent"
  ], required,
  execution: enum["draft", "apply"], optional, default "apply",
  idempotency_key: string, required for every apply request,
  expected_version: integer >= 0, optional,
  acknowledgements: object, optional {
    conflicts: array[string], optional,
    rejections: array[string], optional,
    external_write: boolean, optional
  },
  input: oneOf[
    {payload: object, required},
    {artifact_ref: string, format: enum["json", "jsonl"], required}
  ], required
}
```

`artifact_ref` is allowed only for registry rows that declare bulk input. The
first implementation can allow it for shift assignment, outreach contacts,
and suggestion application. The server validates every row against the same
branch schema.

The `entity` and `operation` pair is a discriminator. The registry must reject
unsupported pairs before any read, queue publish, provider call, or database
transaction. The following payload branches are the full initial contract.

**`caregiver_availability.update` payload:**

```text
{
  caregiver_id: UUID string, required,
  mode: enum["add", "replace", "remove"], required,
  scope: enum["one_off", "weekly"], required,
  state: enum["available", "unavailable"], required for add/replace,
  start_time: ISO-8601 datetime, required for one_off add/replace,
  end_time: ISO-8601 datetime, required for one_off add/replace,
  day_of_week: enum["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"],
    required for weekly add/replace,
  start_minute_of_week: integer 0..10079, required for weekly add/replace,
  end_minute_of_week: integer 1..10080, required for weekly add/replace,
  availability_id: UUID string, required for remove unless a bounded selector is used,
  reason: enum["time_off_request", "shift_offer_decline", "manual", "other"], optional
}
```

One-off fields and weekly fields are mutually exclusive. `replace` preserves
the legacy overlap replacement behavior. `remove` never accepts `state`.

**`caregiver_preference.update` payload:**

```text
{
  caregiver_id: UUID string, required,
  action: enum["answer", "complete", "skip", "reset"], required,
  question_key: string, required for answer,
  value: oneOf[string, integer, number, boolean, array[string]], optional,
  hours_per_week: number >= 0, optional,
  areas: array[string], optional,
  travel_range_miles: number >= 0, optional,
  employment_type: string, optional,
  languages: array[string], optional,
  days_of_week: array[enum["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]], optional,
  service_preferences: array[string], optional
}
```

The survey transition service remains authoritative for valid question keys
and answer shapes. The v3 schema carries the shared envelope and the adapter
passes question-specific fields without creating one tool per question.

**`memory.create|update|discard` payload:**

```text
{
  subject_type: enum["caregiver", "client", "automation"], required,
  subject_id: UUID string, required for caregiver/client,
  memory_id: UUID string, required for update/discard,
  memory_version: integer >= 0, required for update/discard,
  kind: enum["fact", "instruction", "preference"], required for caregiver/client create,
  content: string, required for create/update,
  reason: string, required for automation create/update/discard
}
```

`discard` never accepts new content. Automation memory retains its version
check and audit event behavior.

**`note.create|update` payload:**

```text
{
  note_id: UUID string, required for update,
  content: string, required,
  caregiver_id: UUID string, optional,
  client_id: UUID string, optional,
  shift_id: UUID string, optional
}
```

At least one of `caregiver_id`, `client_id`, or `shift_id` is required.

**`person.update` payload:**

```text
{
  subject_type: enum["caregiver", "client"], required,
  subject_id: UUID string, required,
  fields: object, required {
    agent_instructions: string, optional,
    primary_language: string, optional,
    supported_languages: array[string], optional,
    roles: array[string], optional,
    employment_type: string, optional,
    desired_weekly_hours: number >= 0, optional,
    default_notify_on_fill: boolean, optional,
    care_plan_summary: object {summary: string, bullets: array[string]}, optional,
    client_call_phone_preference: string, optional,
    tag_matching: object {tag: string, importance: string}, optional
  }
}
```

The field allowlist depends on `subject_type`. An empty `fields` object is
invalid. Unknown fields are rejected.

**`shift.mutate` payload:**

```text
{
  action: enum["create", "update", "cancel", "assign", "triage", "add_ehr_note"], required,
  shift_id: UUID string, required for update/cancel/triage/add_ehr_note,
  shift_ids: array[UUID string], required for assign,
  client_id: UUID string, required for create,
  patient_name: string, required for create,
  shift_start_time: ISO-8601 datetime, required for create/update when changed,
  shift_end_time: ISO-8601 datetime, required for create/update when changed,
  address: string, optional,
  frequency: string, optional,
  notes: string, optional,
  caregiver_id: UUID string, required for assign/triage, optional for create,
  unassign_caregiver: boolean, optional for update,
  reassign: boolean, optional for assign,
  rate_selections: object, optional,
  writeback_scope: enum["none", "local", "ehr", "both"], optional,
  cancelled_by: string, required for cancel,
  reason: string, optional,
  stage: string, required for triage,
  note: string, required for add_ehr_note,
  as_activity_note: boolean, optional,
  expected_source_system: string, optional,
  notify: boolean, optional
}
```

The validator applies branch-specific required fields. `assign` accepts a
list. The old one-shift restriction is removed from the model-facing schema,
but policy may cap bulk size.

**`shift_response.resolve` payload:**

```text
{
  source: enum["callout", "confirmation", "review"], required,
  responses: array[{
    shift_id: UUID string, required,
    response: enum["accept", "decline", "interested", "unavailable", "no_response"], required
  }], required, minItems 1,
  contact_attempt_id: UUID string, optional,
  shift_confirmation_id: UUID string, optional,
  reason: string, optional,
  needs_review_owner: UUID string, optional
}
```

`confirmation` requires `shift_confirmation_id` and one response. `review`
requires `contact_attempt_id` and `reason`. The downstream handlers retain
their existing row and dedupe behavior.

**`clock.update` payload:**

```text
{
  action: enum["clock_in", "clock_out", "reminder_response"], required,
  shift_id: UUID string, required,
  clock_hour: integer 0..23, optional,
  clock_minute: integer 0..59, optional,
  clock_date: ISO date, optional,
  timezone: IANA timezone string, optional,
  reminder_type: enum["clock_in", "clock_out"], required for reminder_response,
  expected_minutes_until_clock: integer, optional
}
```

Clock write feature flags remain server policy. Reminder response does not
perform a clock write.

**`communication.send|complete|transfer|classify` payload:**

```text
{
  action: enum["draft", "send", "edit", "reject", "end_call", "transfer", "voicemail"], required,
  channel: enum["sms", "voice_call", "contact_card"], required for message actions,
  conversation_id: UUID string, required for in-app message actions,
  caregiver_id: UUID string, required for caregiver message actions,
  staged_message_id: UUID string, required for edit/reject and send-existing,
  content: string, required for sms draft/send/edit,
  instructions: string, required for voice_call draft/send,
  opening_line: string, optional,
  target_language: string, optional,
  send_anyway: boolean, optional,
  farewell: string, optional for end_call,
  destination: string, required for transfer,
  reason: string, optional
}
```

`contact_card` requires no free text and sends the generated contact-card
link. `voice_call` requires instructions. `end_call`, `transfer`, and
`voicemail` require live call context and do not accept message fields.

**`outreach` payload:**

```text
{
  action: enum[
    "create", "update_structure", "update_metadata", "send",
    "add_contacts", "set_status", "update_suggestions", "compute_suggestions",
    "apply_suggestions", "fill_shifts", "cancel_shifts", "notify",
    "offer_shifts", "record_offer", "record_responses", "express_interest",
    "monitor_start", "monitor_stop"
  ], required,
  outreach_id: UUID string, required except create and offer_shifts when a new outreach is created,
  shift_ids: array[UUID string], optional,
  caregiver_ids: array[UUID string], optional,
  caregiver_shift_ids: array[object {caregiver_id: UUID string, shift_ids: array[UUID string]}], optional,
  delivery: enum["sms", "voice", "both"], optional,
  suggestions: array[object {
    caregiver_id: UUID string,
    shift_ids: array[UUID string], optional,
    groups: array[string], optional
  }], optional,
  suggestion_mode: enum["append", "overwrite", "remove", "merge"], optional,
  draft_id: UUID string, optional,
  min_options_per_unit: integer >= 0, optional,
  limit: integer >= 1, optional,
  max_suggestions_per_unit: integer >= 1, optional,
  weights: object, optional,
  filters: object, optional,
  status: enum["draft", "active", "paused", "completed", "cancelled"], optional,
  reason: string, optional,
  instructions: string, optional,
  opening_line: string, optional,
  templates: object, optional,
  notify_client_on_fill: boolean, optional,
  schedule: object, optional,
  grouping: object, optional,
  auto_assign: object, optional,
  callout_id: UUID string, optional,
  caregiver_id: UUID string, optional,
  assign_without_response: boolean, optional,
  rate_selections: object, optional,
  acknowledged_conflicts: array[string], optional,
  acknowledged_rejections: array[string], optional,
  message: string, optional,
  include_assigned: boolean, optional,
  include_unassigned: boolean, optional,
  conditions: array[string], optional,
  pre_approved_actions: array[string], optional,
  action_reason: string, optional,
  outreach_type: string, optional,
  urgency: string, optional
}
```

The operation discriminator adds branch validation. For example, `send`
requires caregiver-shift pairs and contact acknowledgements; `monitor_start`
requires conditions; `update_metadata` rejects shift IDs; and
`record_responses` requires response objects. The large shape is deliberate:
these actions share one outreach service and one high-risk policy boundary.

**`organization_policy.update` payload:**

```text
{
  policy: enum["general_settings", "callout_settings", "excluded_caregivers", "recommendation_defaults", "tag_matching", "filter_rule"], required,
  action: enum["set", "add", "remove", "replace"], required,
  caregiver_ids: array[UUID string], optional,
  general_settings: object {
    auto_send_callout_decline_response: boolean, optional,
    positive_shift_confirmation_reply: string, optional,
    negative_shift_confirmation_reply: string, optional,
    unclear_shift_confirmation_reply: string, optional,
    auto_send_all_replies: boolean, optional
  }, optional,
  callout_settings: object {
    auto_assign_first_responder_default: boolean, optional,
    allow_auto_assign: boolean, optional,
    skip_busy_caregivers: boolean, optional
  }, optional,
  recommendation_defaults: object, optional,
  tag_matching: object {client_id: UUID string, tag: string, importance: string}, optional,
  filter_rule: object {
    outreach_type: string,
    entity_type: enum["caregiver", "client", "coordinator"],
    entity_id: UUID string, optional,
    entity_tag: string, optional,
    filter_mode: enum["allow", "skip"], optional,
    exception_entity_ids: array[UUID string], optional
  }, optional
}
```

The recommendation defaults object uses the bounded fields already enforced
by its service. It must not become an unvalidated JSON escape hatch.

**`playbook.update` payload:**

```text
{
  action: enum["request", "patch", "natural_language"], required,
  playbook_id: UUID string, required for patch/natural_language,
  expected_version: integer >= 0, required,
  request_text: string, required for request/natural_language,
  patch: object {
    title: string, optional,
    usage_text: string, optional,
    instructions: string, optional,
    surfaces: array[string], optional,
    enabled: boolean, optional
  }, optional
}
```

**`relationship.assign|remove` payload:**

```text
{
  relationship: enum["client_care_coordinator", "outreach_subscriber"], required,
  client_id: UUID string, required for client_care_coordinator,
  care_coordinator_id: UUID string, required for client_care_coordinator,
  outreach_id: UUID string, required for outreach_subscriber,
  user_id: UUID string, required for outreach_subscriber
}
```

**`sync.trigger` payload:**

```text
{
  entity_type: enum["client", "caregiver"], required,
  client_id: UUID string, required when entity_type=client,
  caregiver_id: UUID string, required when entity_type=caregiver
}
```

**`feedback.create` payload:**

```text
{
  target: enum["agent_run", "outreach_suggestion"], required,
  message: string, required for agent_run,
  category: string, optional,
  outreach_id: UUID string, required for outreach_suggestion,
  caregiver_id: UUID string, required for outreach_suggestion,
  sentiment: enum["positive", "negative", "unclear"], required for outreach_suggestion,
  reason: string, required for outreach_suggestion
}
```

**`report.create` payload:**

```text
{
  source: enum["run_event", "manual"], required,
  report_id: UUID string, optional,
  title: string, optional,
  columns: array[string], optional,
  rows: array[object[string, string|number|boolean|null]], optional
}
```

Manual reports require `columns` and `rows`. Run-event reports require
`report_id`.

**`conversation.complete|record_intent` payload:**

```text
{
  action: enum["complete", "record_intent"], required,
  reason: string, optional,
  intent: enum[
    "availability", "unavailability", "clock_in", "clock_out",
    "callout", "shift_confirmation", "shift_cancellation",
    "preference_survey", "open_shifts", "contact_card", "other"
  ], required for record_intent
}
```

The implementation must reconcile this proposed intent enum with the existing
SMS intent registry before migration. Unknown legacy values must return a
typed mapping error, not be coerced to `other`.

**Common result schema:** Every `apply` returns:

```text
{
  status: enum["applied", "drafted", "queued", "rejected"], required,
  entity: string, required,
  operation: string, required,
  idempotency_key: string, required,
  affected_ids: array[UUID string], required,
  downstream_job_id: string, optional,
  draft_id: UUID string, optional,
  warnings: array[string], required
}
```

The result is intentionally uniform. Domain adapters may add a bounded
`details` object, but they must not return unbounded provider payloads.

### `conversation.check_in`

**Purpose:** Schedule or cancel a durable check-in for the current
conversation. This replaces the two current v3 poke names and the legacy
`schedule_self_poke` name.

**Argument schema:**

```text
conversation.check_in = {
  action: enum["schedule", "cancel"], required,
  reason: string, required for schedule,
  instructions: string, required for schedule,
  delay_minutes: integer, optional for schedule,
  at_local_time: local ISO datetime string, optional for schedule,
  repeat_every_minutes: integer, optional for schedule,
  repeat_count: integer, optional for schedule,
  group_id: string, required for cancel
}
```

Exactly one of `delay_minutes` and `at_local_time` is required for schedule.
The existing floor, horizon, pending-fire, and lifetime limits remain in the
scheduler. The current v3 schema and implementation are at
`phoebe_v3_agent/tools/poke/tools.py:216-385` and
`phoebe_v3_agent/tools/poke/tools.py:388-465`.

**Capability lost:** Schedule and cancel no longer have separate tool names.
The action union must be strict. The scheduler still returns the same budget,
fire, and pending-poke details.

### Tools that remain unchanged

- `run_bash` remains the workspace command tool with `command`, optional
  `timeout_s`, and optional `presentation`
  (`phoebe_v3_agent/tools/bash/tool.py:232-275`). It cannot access the live
  database or network.
- `query` remains the read-only SQL tool with required `sql` and `description`
  (`phoebe_v3_agent/tools/query/tool.py:57-149`). It does not gain writes.
- `show_dynamic_ui` remains the one-decision interaction tool with its current
  input and presentation unions (`phoebe_v3_agent/tools/interaction/tool.py:24-250`).

## Migration mapping

Every audited write name maps to a `write` entity and operation. Legacy
surface adapters supply explicit IDs and preserve existing gates. A slash in
the audit is split into separate rows here.

| old tool | new tool and operation | argument mapping |
|---|---|---|
| `set_caregiver_availability` (SMS) | `write caregiver_availability.update` | `caregiver_id` from context; `mode=replace`, `scope=one_off`, `state=available`; map start/end |
| `set_caregiver_unavailability` (SMS) | `write caregiver_availability.update` | same; `state=unavailable`; map reason |
| `set_caregiver_weekly_availability` | `write caregiver_availability.update` | `mode=replace`, `scope=weekly`, `state=available`; map day and minutes |
| `set_caregiver_weekly_unavailability` | `write caregiver_availability.update` | same; `state=unavailable` |
| `record_caregiver_preference_answer` | `write caregiver_preference.update` | copy action, question_key, and question-specific values |
| `handle_clock_in_tool` | `write clock.update` | `action=clock_in`; copy shift and optional clock fields |
| `handle_clock_out_tool` | `write clock.update` | `action=clock_out`; copy shift and optional clock fields |
| `handle_clock_in_reminder_response_tool` | `write clock.update` | `action=reminder_response`, `reminder_type=clock_in`; copy expected minutes |
| `handle_clock_out_reminder_response_tool` | `write clock.update` | `action=reminder_response`, `reminder_type=clock_out`; copy expected minutes |
| `send_contact_card_tool` | `write communication.send` | `action=send`, `channel=contact_card`; map caregiver and conversation context |
| `mark_conversation_complete_tool` | `write conversation.complete` | `action=complete`; map reason |
| `record_intent_tool` | `write conversation.record_intent` | `action=record_intent`; map intent through explicit enum adapter |
| `save_memory` | `write memory.create` | `subject_type=caregiver`, map note to content and kind= fact |
| `handle_callout_response_tool_v2` | `write shift_response.resolve` | `source=callout`; map attempt and shift responses |
| `handle_upcoming_shift_cancellation_tool` | `write shift.mutate` | `action=cancel`; map shift_id and reason |
| `handle_shift_confirmation_response_tool` | `write shift_response.resolve` | `source=confirmation`; map confirmation ID and accept/decline response |
| `offer_open_shifts_tool` | `write outreach.mutate` | `action=offer_shifts`; map date window to filters |
| `end_call` | `write communication.complete` | `action=end_call`; map live call context and farewell |
| `transfer_call` | `write communication.transfer` | `action=transfer`; map resolved destination |
| `mark_as_voicemail` | `write communication.classify` | `action=voicemail`; map live call context |
| `handle_callout_response` | `write shift_response.resolve` | `source=callout`; resolve voice context to explicit IDs |
| `handle_outreach_response` | `write shift_response.resolve` | `source=callout`; resolve inbound claim to explicit IDs |
| `set_caregiver_availability` (voice) | `write caregiver_availability.update` | `mode=replace`, `scope=one_off`, `state=available` |
| `set_caregiver_unavailability` (voice) | `write caregiver_availability.update` | `mode=replace`, `scope=one_off`, `state=unavailable` |
| `mark_declined_shifts_unavailable` | `write caregiver_availability.update` | one update per declined interval; `state=unavailable`, `reason=shift_offer_decline` |
| `handle_shift_confirmation_response` (voice) | `write shift_response.resolve` | `source=confirmation`; resolve pending confirmation ID |
| `handle_upcoming_shift_cancellation` (voice) | `write shift.mutate` | `action=cancel`; preserve transfer threshold policy |
| `record_caregiver_preference_answer_voice` | `write caregiver_preference.update` | copy action, question_key, and typed values |
| `record_shift_discovery_offer` | `write outreach.mutate` | `action=record_offer`; map outreach and matched shifts |
| `record_shift_discovery_responses` | `write outreach.mutate` | `action=record_responses`; map response list |
| `express_shift_interest` | `write outreach.mutate` | `action=express_interest`; map caregiver and shift context |
| `save_note` | `write note.create` | map content and one or more subject IDs |
| `save_automation_memory` | `write memory.create` | `subject_type=automation`; map kind, content, reason |
| `update_automation_memory` | `write memory.update` | map memory ID, version, content, reason |
| `discard_automation_memory` | `write memory.discard` | map memory ID, version, reason |
| `add_caregiver_weekly_availability` | `write caregiver_availability.update` | `mode=add`, `scope=weekly`, `state` from status; map minute range |
| `add_caregiver_one_off_availability` | `write caregiver_availability.update` | `mode=add`, `scope=one_off`, map status and interval |
| `delete_caregiver_availability` | `write caregiver_availability.update` | `mode=remove`; map availability ID and recurrence to scope |
| `save_caregiver_memory_instruction` | `write memory.create` | `subject_type=caregiver`, `kind=instruction` |
| `update_caregiver_record` | `write person.update` | `subject_type=caregiver`; move allowlisted fields under fields |
| `save_client_memory_instruction` | `write memory.create` | `subject_type=client`, `kind=instruction` |
| `update_client_record` | `write person.update` | `subject_type=client`; map profile and care-plan fields |
| `update_client_phone_preference` | `write person.update` | `subject_type=client`; fields.client_call_phone_preference |
| `assign_client_care_coordinator` | `write relationship.assign` | relationship=client_care_coordinator; map two IDs |
| `remove_client_care_coordinator` | `write relationship.remove` | same relationship and IDs |
| `approve_staged_message` | `write communication.send` | `action=send`, existing staged ID, delivery=send; map updated content |
| `reject_staged_message` | `write communication.send` | `action=reject`; map staged ID and reason |
| `edit_staged_message_content` | `write communication.send` | `action=edit`; map staged ID and content |
| `send_one_off_sms_in_conversation` | `write communication.send` | `action=send`, `channel=sms`; map content, language, send_anyway |
| `place_one_off_voice_call_in_conversation` | `write communication.send` | `action=send`, `channel=voice_call`; map instructions/opening line |
| `trigger_ehr_sync` | `write sync.trigger` | map entity_type and exactly one entity ID |
| `draft_agent_outreach` | `write outreach.mutate` | `action=create`, `execution=draft`; map shift IDs and draft fields |
| `manage_outreach_start_with_contacts` | `write outreach.mutate` | `action=send`; map outreach, caregiver-shift pairs, delivery, acknowledgements |
| `manage_outreach_add_contact_attempts` | `write outreach.mutate` | `action=add_contacts`; map caregiver IDs and shift IDs |
| `add_outreach_suggestions` | `write outreach.mutate` | `action=update_suggestions`; map suggestions and mode |
| `manage_outreach_remove_suggestions` | `write outreach.mutate` | `action=update_suggestions`, `suggestion_mode=remove` |
| `manage_outreach_set_status` | `write outreach.mutate` | `action=set_status`; map action to status |
| `update_agent_outreach` | `write outreach.mutate` | `action=update_structure`; map shift groups, care plans, and auto-assign fields |
| `update_outreach_metadata` | `write outreach.mutate` | `action=update_metadata`; map instructions, opening line, templates |
| `fill_callout_shifts` | `write shift.mutate` | `action=assign`; map callout ID, shifts, caregiver, notifications, EHR option |
| `notify_caregivers` | `write outreach.mutate` | `action=notify`; map caregiver-shift pairs and custom messages |
| `notify` | `write outreach.mutate` | `action=notify`; use coordinator notification variant |
| `override_contact_attempt_response` | `write shift_response.resolve` | `source=review`; map attempt, shift, response, reason |
| `submit_outreach_suggestion_feedback` | `write feedback.create` | `target=outreach_suggestion`; map outreach, caregiver, sentiment, reason |
| `add_outreach_subscriber` | `write relationship.assign` | relationship=outreach_subscriber; map outreach and user IDs |
| `remove_outreach_subscriber` | `write relationship.remove` | same relationship and IDs |
| `monitor_outreach` | `write outreach.mutate` | `action=monitor_start`; map reason, conditions, pre-approved actions |
| `stop_agent_monitoring` | `write outreach.mutate` | `action=monitor_stop`; map outreach and reason |
| `schedule_self_poke` | `conversation.check_in` | `action=schedule`; map delay, reason, instructions, recurrence |
| `assign_shifts_to_caregiver` | `write shift.mutate` | `action=assign`; map shift IDs, caregiver, reassignment, rates |
| `create_shift` | `write shift.mutate` | `action=create`; map client, patient, times, address, frequency, notes |
| `update_shift` | `write shift.mutate` | `action=update`; map shift fields and writeback scope |
| `add_shift_note_to_ehr` | `write shift.mutate` | `action=add_ehr_note`; map shift, note, activity flag, source system |
| `cancel_shift` | `write shift.mutate` | `action=cancel`; map shift, cancelled_by, reason |
| `update_shift_triage_stage` | `write shift.mutate` | `action=triage`; map shift, caregiver, stage, reason |
| `cancel_callout_shifts` | `write outreach.mutate` | `action=cancel_shifts`; map outreach, shift IDs, notifications, reasons |
| `update_organization_general_settings` | `write organization_policy.update` | `policy=general_settings`, `action=set`; map allowlisted fields |
| `update_callout_settings` | `write organization_policy.update` | `policy=callout_settings`, `action=set` |
| `update_callout_excluded_caregivers` | `write organization_policy.update` | `policy=excluded_caregivers`; map action and caregiver IDs |
| `update_recommendation_defaults` | `write organization_policy.update` | `policy=recommendation_defaults`, `action=set`; map bounded fields |
| `set_tag_matching_importance` | `write person.update` | `subject_type=client`; fields.tag_matching |
| `apply_reliability_policy_request_tool` | `write playbook.update` | `action=request`; map expected version and request text |
| `update_playbook_item_tool` | `write playbook.update` | `action=patch`; map playbook ID, version, and patch fields |
| `apply_playbook_natural_language_edit` | `write playbook.update` | `action=natural_language`; map playbook ID, version, request text |
| `update_outreach_filter_rule` | `write organization_policy.update` | `policy=filter_rule`; map rule fields and action |
| `submit_general_agent_feedback` | `write feedback.create` | `target=agent_run`; map message and category |
| `generate_report` | `write report.create` | map report source, handle, columns, and bounded rows |
| `get_outreach_suggestion_drafts` | `write outreach.mutate` | `action=compute_suggestions`; map filters, limits, weights, and outreach ID |
| `apply_outreach_suggestion_drafts` | `write outreach.mutate` | `action=apply_suggestions`; map draft ID and suggestion mode |
| `merge_recommendation_fork_suggestion` | `write outreach.mutate` | `action=update_suggestions`, `suggestion_mode=merge`; map suggestions and acknowledgements |

The 90 audited names above all map to `write` except
`schedule_self_poke`, which maps to `conversation.check_in`. The current v3
`schedule_poke` and `cancel_poke` map to the same check-in operation and are
removed as callable names after parity tests pass.

## Open questions

- Should persistent staged messages use `execution=draft|apply` in the same
  `communication.send` branch, or should staged artifacts keep a separate
  internal service API while the model still sees one tool?
- Which write operations require a durable human approval wait before v3
  ships? The current bundle sets `tool_approval_config=None`, and that is safe
  only while v3 has no business writes.
- Should `artifact_ref` bulk input ship in the first write slice? It reduces
  repeated calls, but it adds file validation, expiry, and audit work.
- Which exact values belong in the caregiver preference `question_key` and
  SMS `intent` enums? The audit names their fields but does not provide the
  complete source enums.
- Should `communication` include `end_call`, `transfer`, and `voicemail`, or
  should live-call control remain a separate voice runtime boundary? One write
  door favors inclusion. Live call provider retries favor a dedicated adapter.
- Should `outreach` keep `monitor_start` and `monitor_stop` in the same union?
  They share outreach scope but create and cancel autonomous follow-up state.
- What approval and idempotency result should a queued worker return when the
  worker has accepted a payload but has not completed the external write?
- Should legacy context-only voice tools resolve IDs in a voice adapter, or
  should the voice runtime pass explicit IDs before entering the v3 write
  registry?
- Which zero-use tools should migrate in the first slice? The audit reports
  zero use for several discovery, setting, and create paths. Keeping their
  registry rows costs little, but enabling their policy may still be risky.

## Non-goals

- This design does not change read tools, including `query`.
- This design does not change `run_bash`, workspace security, or artifact
  rendering.
- This design does not redesign `show_dynamic_ui` or interaction cards.
- This design does not migrate `phoebe_admin_agent*` or staff-only Slack
  tools.
- This design does not add post-call voice actions that are not LLM tools,
  such as QA clock writeback or receptionist item capture. The audit lists
  those separately (`agent-write-tools-audit-epic.md:1281-1294`).
- This design does not change database tables, worker payloads, EHR provider
  contracts, or business rules. Adapters must preserve them.
- This design does not solve the missing `llm_messaging_engine` path. The
  audit records that current SMS code uses `sms_conversation_engine`
  (`agent-write-tools-audit-epic.md:37-39`).
- This design does not define the complete approval UI or durable approval
  event schema.
- This design does not promise that one model call can safely perform every
  bulk mutation. Policy may cap batch size or require `execution=draft`.
