---
type: reference
tags: [phoebe, admin-agent, v3, tools, plan]
created: 2026-08-19
updated: 2026-08-19
---

> Deliverable of PHO-15864-TOOLS-PLAN worker (2026-08-19). Copied from /tmp/pho-15864-tools-plan.md.

## DECISIONS LOCKED (Henry, 2026-08-19 — recorded on PHO-15864)

**Scope reframe (supersedes decision 12, amends 3/10):** phoebe_admin_agent_v3 is Henry's SOLO dev project — no users, no other engineers, NO cutover planning. Goal: capability parity with the existing admin agent. Slack tools/functionality deferred entirely until core functionality is verified.

- d1 query input: catalog expression, not raw SQL (per rec)
- d2 write shape: one `write` door, discriminated operation union (per rec)
- d3 first writes: notes + settings ONLY — slack op deferred (amended)
- d4 approvals: KEEP v2's 15-name approval split as-is (deviates from rec — no risk-based rethink now)
- d5 query bounds: tight v3-style (50 inline / 10k workspace / 10 MiB / 5s) (per rec)
- d6 retention: transient by default, durable needs explicit intent (per rec)
- d7 compaction failure: fail once, degrade gracefully, no retry (per rec)
- d8 dynamic MCP: dropped from first ladder (per rec)
- d9 subagents: child run + immutable snapshot only (per rec)
- d10 surface order: interactive first; slack deferred indefinitely (amended)
- d11 ledger rule: explicit Henry approval per mutation-class/user-visible redesign (per rec)
- d12 old runs/cutover: DEFERRED — see scope reframe

Ladder impact: rung 9 (slack) and rung 13 (drain) deferred; rungs 1-8, 10-12 proceed under parity-not-cutover framing.

# pho-15864 admin agent v3 tools plan

source of truth: `origin/main` at `9997a39f43`. this is a design only. no
source files, commits, or runtime wiring change in this task.

## 1. inventory

### accounting and approval basis

the v3 port ledger has 190 named behaviors. `port_ledger.json` records 190
tool keys, while the top-level live digest records 188 tool names. The two
digest-only names are `post_daily_tool_quality_review_to_slack` and
`post_to_slack_thread`; this is an accounting gap to explain, not permission
to drop either behavior. See
`libraries/python/phoebe_admin_agent_v3/port_ledger.json:1-5` and
`phoebe_admin_agent/admin_runtime_characterization_digest.json` top-level
tools.

the top-level live digest marks 15 names as approval-required:

`activate_admin_page_version`, `add_admin_call_routing_destination`,
`add_admin_phone_forwarding`, `configure_admin_page_refresh_schedule`,
`create_admin_sales_agreement`, `invite_admin_organization_user`,
`join_slack_channel`, `manage_admin_agent_automation`, `mute_alert_thread`,
`refresh_admin_page_data`, `update_admin_call_routing_destination`,
`update_admin_organization_feature_flags`,
`update_admin_organization_settings`, `update_admin_phone_forwarding`, and
`upsert_org_territory`.

the remaining top-level names are audit-only or read-only in that digest. The
admin-subagent mode has zero approval-required names because it inherits a
child policy. This is current behavior, not the recommended v3 policy. The
approval set is in the live digest under each mode's
`approval_required_tool_names` field. The v2 contract defines the two classes
and the side-effect mapping in
`libraries/python/phoebe_admin_agent/contracts/admin_tool_contracts.py:147-206`.

usage weight is available only for 47 hot names. The ranking comes from the
2026-07-01 to 2026-07-08 seven-day measurement window, plus run-data verbs and
split call-recording verbs. The default hot count is 47. See
`libraries/python/phoebe_admin_agent/admin_tool_tiering.py:1-32,37-92`.

### query and read

approval class: read-only unless a name is also listed in the live digest's
approval set. Usage weight is `hot` where noted; all other names are `cold` or
have no measured call count.

the complete query/read inventory is:

- `check_admin_core_api_health`, `compare_admin_agent_source_health` (`cold`),
  `compare_admin_github_refs` (`hot`), `compare_agent_runs` (`cold`),
  `describe_admin_page_snowflake_schema`, `describe_table` (`hot`),
  `diagnose_kickoff_readiness`, `diff_run_data` (`hot`),
  `fetch_live_ehr_record`, `find_admin_settings_location`,
  `find_codebase_references`, `report_missing_tool`,
  `inspect_codebase_map` (`hot`), `inspect_codebase_relationships`,
  `inspect_codebase_wiki`, `inspect_outreach_start_actions`,
  `localize_codebase_issue`, `localize_runtime_evidence_to_code`,
  `map_admin_work_item_to_codebase`, `outline_codebase_file` (`hot`),
  `review_codebase_evidence`.
- `get_admin_account_health_metrics`, `get_admin_account_health_rollups`,
  `get_admin_core_account_context` (`hot`), `get_admin_core_tam_account`,
  `get_admin_crm_context`, `get_admin_github_file` (`hot`),
  `get_admin_meeting_context`, `get_admin_organization_feature_flags`,
  `get_admin_organization_settings`, `get_admin_phone_routing`,
  `get_admin_posthog_account_health_users`, `get_agent_court_case` (`hot`),
  `get_core_account_analysis_log`, `get_core_account_book`,
  `get_core_account_dossier`, `get_core_email_message`,
  `get_core_email_thread`, `get_core_funnel_snapshot`,
  `get_core_intercom_conversation`, `get_current_incident_io_on_call`,
  `get_onboarding_checklist_state`, `get_org_context_index` (`hot`),
  `get_org_ehr_state`, `get_slack_channel_info`, `get_slack_thread`,
  `inspect_admin_calendar_event`, `inspect_admin_feature_flag_rollout`,
  `inspect_admin_github_commit`, `inspect_admin_github_pull_request` (`hot`),
  `inspect_admin_github_workflow_run`, `inspect_admin_integration_migration`
  (`hot`), `inspect_admin_linear_document`, `inspect_admin_linear_issue` (`hot`),
  `inspect_admin_notion_page`, `inspect_admin_organization_membership`,
  `inspect_admin_tool_registry_fixture`, `inspect_agent_run_trace` (`hot`).
- `investigate_braintrust_evals`, `investigate_codebase` (`hot`),
  `investigate_datadog_alert`, `investigate_logfire_records` (`hot`),
  `investigate_outreach_agent_run` (`hot`), `investigate_posthog_analytics`,
  `investigate_token_alert`, `load_org_context_summary` (`hot`),
  `resolve_admin_investigation_context` (`hot`), `resolve_core_account_owner`,
  `resolve_org_territories`, `resolve_org_territory`,
  `resolve_source_slack_thread_context`, `run_admin_investigation_playbook`,
  `run_admin_parallel_investigation`, `survey_org_feature_adoption`,
  `sweep_agent_health`, `triage_agent_run_observability`.
- `list_admin_calendar_events`, `list_admin_calendars`,
  `list_admin_core_accounts` (`hot`), `list_admin_core_events`,
  `list_admin_database_queries`, `list_admin_github_branches`,
  `list_admin_github_commits`, `list_admin_github_repositories`,
  `list_admin_github_tags`, `list_admin_github_workflow_runs`,
  `list_admin_pages`, `list_admin_sales_agreement_templates`,
  `list_agent_court_cases`, `list_core_account_emails`, `list_org_territories`,
  `list_slack_channels`, `list_sub_agents`, `lookup_slack_user`,
  `read_admin_note` (`hot`), `read_admin_note_revisions` (`hot`),
  `read_admin_page`, `read_call_recording_transcript` (`hot`),
  `read_codebase_source_range` (`hot`), `read_run_data` (`hot`),
  `search_admin_core_tam_accounts` (`hot`),
  `search_admin_github_pull_requests` (`hot`),
  `search_admin_linear_issues` (`hot`), `search_admin_notes` (`hot`),
  `search_admin_notion` (`hot`), `search_call_recording_index` (`hot`),
  `search_call_recording_transcript` (`hot`),
  `search_codebase_knowledge` (`hot`), `search_codebase_source` (`hot`),
  `search_codebase_symbols` (`hot`), `search_feature_catalog` (`hot`),
  `search_index`, `search_notes`, `search_run_data` (`hot`),
  `search_schema`, `search_saved_admin_investigations`,
  `search_slack_messages`, `slice_json` (`hot`),
  `summarize_admin_work_plan`, `validate_admin_capability_readiness` (`hot`),
  `review_admin_tool_quality`.
- `query_admin_database` (`hot`), `run_admin_database_query`,
  `resolve_admin_investigation_context` (`hot`), and `describe_table` (`hot`)
  are named here by capability, but their overlap is intentional in this
  inventory. They are separate v2 definitions today. The database module
  defines four related tool names, a 100,000 row ceiling, a 60-second
  statement timeout, optional public result caps, and a 200,000-byte curated
  query cap. See
  `libraries/python/phoebe_admin_agent/admin_database_query.py:98-117` and
  `:3511-3679`.

### write and mutation

approval class: the 15 names listed above are `approval_required`. The other
mutating names are `audit_only` today. That split is too coarse for v3. The
complete write inventory is:

`activate_admin_page_version`, `add_admin_call_routing_destination`,
`add_admin_phone_forwarding`, `apply_admin_notion_write`,
`apply_linear_workflow_mutation`, `classify_organization_shifts`,
`confirm_admin_note`, `configure_admin_page_refresh_schedule`,
`create_admin_page_version_from_sandbox`, `create_admin_sales_agreement`,
`create_or_resolve_general_agent_probe_org`, `delete_slack_message`,
`invite_admin_organization_user`, `manage_admin_agent_automation`,
`mark_account_reviewed`, `mute_alert_thread`,
`post_account_health_threads_to_slack`, `post_admin_github_pr_review`,
`post_daily_tool_quality_review_to_slack`, `post_to_slack_channel`,
`post_to_slack_thread`, `preview_admin_feature_flag_updates`,
`preview_admin_notion_write`, `preview_head_justice_digest`,
`propose_skill_update`, `queue_agent_court_justice`,
`reconcile_outreach_suggestions`, `refresh_admin_page_data`,
`retire_admin_note`, `schedule_slack_message`, `set_call_recording_tags`,
`submit_call_analysis`, `update_admin_call_routing_destination`,
`update_admin_note`, `update_admin_organization_feature_flags`,
`update_admin_organization_settings`, `update_admin_phone_forwarding`,
`update_call_analysis`, `update_slack_message`, `upload_file_to_slack`,
`upsert_org_territory`, `write_admin_note`, and
`request_orchard_code_change_task`.

v2 has one registry but many mutation scopes and side-effect policies. The
registry imports every definition centrally and also imports six subagent
definitions from `phoebe_event_agent`. See
`libraries/python/phoebe_admin_agent/admin_tool_registry.py:1-8,231-331` and
`contracts/admin_tool_contracts.py:112-206`.

### bash and sandbox

approval class: `audit_only` for sandbox execution in the current surface.
The complete sandbox inventory is:

`create_code_sandbox`, `destroy_sandbox`, `export_sandbox_diff`,
`list_sandboxes`, `load_artifact_into_sandbox`, `read_sandbox_file`,
`run_code_in_sandbox`, `run_sandbox_command`, and `write_sandbox_file`.

the v3 harness already decides that bash is a framework-level execution layer,
not an agent-specific tool implementation. See
`libraries/python/phoebe_v3_agent/ORGANIZATION.md` bash execution section. The
admin rewrite should expose one admin-mounted bash capability and one
run-scoped workspace, rather than carrying this nine-name lifecycle.

### artifacts and receipts

approval class: audit-only or internal runtime behavior. The complete
artifact/receipt inventory is:

`emit_admin_chart_artifact`, `emit_admin_csv_artifact`,
`emit_admin_file_artifact`, `emit_admin_image_artifact`,
`load_artifact_into_sandbox`, `read_tool_result_by_id`, `record_run_turn`, and
`replay_recorded_turn`.

v2 also emits artifact references as part of nearly every controlled tool
result. The contract has separate audit references, sandbox receipts, artifact
sources, artifact refs, truncation markers, and compacted results. See
`libraries/python/phoebe_admin_agent/contracts/admin_tool_contracts.py:282-486`.

### mcp and dynamic

`discover_admin_mcp_tool_definitions` is a v2 runtime capability, but its
discovered names are not fixed entries in the 190-name v3 ledger. It loads
allowlisted provider definitions at runtime and then injects them into the
registry. See `libraries/python/phoebe_admin_agent/tools/admin_mcp_tools.py:112-154,298`
and `capability_registry.py:169-251`.

the characterization suite uses fake MCP definitions. That makes this a
dynamic extension seam, not a stable admin tool contract. See
`admin_runtime_characterization_test.py:44,110,371-377`.

### slack and surface-specific

the slack inventory is:

`delete_slack_message`, `get_slack_channel_info`, `get_slack_thread`,
`join_slack_channel`, `list_slack_channels`, `lookup_slack_user`,
`mute_alert_thread`, `post_account_health_threads_to_slack`,
`post_daily_tool_quality_review_to_slack`, `post_to_slack_channel`,
`post_to_slack_thread`, `reply_to_slack_thread`,
`resolve_source_slack_thread_context`, `schedule_slack_message`,
`search_slack_messages`, `update_slack_message`, and `upload_file_to_slack`.

the v2 slack registry builds each definition through one helper, but keeps
separate read, post, reply, upload, schedule, delete, join, and mute
semantics. Approval is required for join and mute, while most other writes
are audit-only. See
`libraries/python/phoebe_admin_agent/admin_slack_registry.py:119-165,168-407`.

### subagent and orchestration

the subagent inventory is:

`create_sub_agent`, `create_sub_agents`, `dump_sub_agent`, `list_sub_agents`,
`message_sub_agent`, `run_admin_parallel_investigation`,
`run_general_agent_probe`, `create_or_resolve_general_agent_probe_org`,
`get_agent_court_case`, `list_agent_court_cases`,
`queue_agent_court_justice`, and `run_agent_court_judge`.

six of these definitions are imported from `phoebe_event_agent`, which
violates the v3 sibling-agent boundary. See
`phoebe_admin_agent/admin_tool_registry.py:231-238` and
`phoebe_admin_agent_v3/ORGANIZATION.md:70-75,197-198`.

### misc

the remaining quality, call, note, page, and investigation controls are
already listed above by their capability. No second catch-all registry should
be created. The v2 `AdminToolRegistry.as_skills()` creates one skill per
definition and then one aggregate group. See
`libraries/python/phoebe_admin_agent/admin_tools.py:66-145`. This is a
mechanical mounting shape, not a useful product taxonomy.

## 2. verdict per family

| family | verdict | reason |
| --- | --- | --- |
| query/read | redesign-shape | v2 has overlapping database, schema, catalog, investigation, and source tools. `admin_database_query.py:98-117` exposes four related database names. The result types and caps vary by definition. v3 should keep capability coverage but present fewer semantic entry points. |
| write/mutation | redesign-shape | v2 spreads approval across 15 names and makes the rest audit-only. `AdminToolDefinition` carries caps, mutation scopes, approval class, and side-effect policy in one large contract (`contracts/admin_tool_contracts.py:489-679`). One typed operation registry should replace model-visible tool sprawl. |
| bash/sandbox | keep-concept-rewrite-clean | sandbox execution is needed, but the nine-name lifecycle is an implementation leak. v3 explicitly places bash in the shared framework boundary. |
| artifacts/receipts | redesign-shape | v2 has durable artifacts, sandbox receipts, compacted results, full-result readback, and cap markers. The split creates multiple identities for one tool output (`contracts/admin_tool_contracts.py:282-486`). Use one evidence reference with separate transient and durable backends. |
| mcp/dynamic | drop as a generic production surface | the discovered names are not ledger-stable, and the current path is tested with fake dynamic definitions. Retain only a named, allowlisted provider adapter when a real admin use case has an owner. |
| slack | redesign-shape | keep slack as a surface skill, but merge read/search and mutation operations into typed family tools. Preserve approval by operation, not by legacy function name. |
| subagent/orchestration | redesign-shape | v2 imports subagent definitions from a sibling agent and exposes parallel investigation, court, probe, and lifecycle verbs. Use one child-run/delegate contract, with named workflow skills for court and probe behavior. |
| notes/pages/settings | merge-with-write | notes, page versions, settings, routing, and feature flags are writes or reads over domain adapters. They should not each become a new harness concept. |
| call analysis and quality | redesign-shape | call analysis, quality review, and account health overlap in data access and follow-up writes. Keep domain skills, but make them compose query, write, and evidence primitives. |
| codebase and external research | merge-with-query/read | source, github, linear, posthog, datadog, logfire, and codebase investigation are read adapters. Keep provider-specific skills only where their query language or authorization differs. |
| single-consumer and incident relics | drop unless a current surface test proves need | the ticket decision says to remove single-consumer and incident relics. Every drop must get a ledger disposition and reason. `phoebe_admin_agent_v3/ORGANIZATION.md:16-32` makes this the expected rewrite posture. |

## 3. core tool set design

### proposed package layout

follow the v3 admin organization rules. The root remains docs, governance,
ledger, tests, and prompt data. See
`phoebe_admin_agent_v3/ORGANIZATION.md:106-172`.

- `core/context.py`: immutable `AdminRunContext` with organization, user,
  timezone, mode, run id, surface, environment, and authorization scope.
- `core/contracts.py`: small result, error, evidence-reference, and operation
  identity models. No imports from `tools/`, `skills/`, or surfaces.
- `core/policy.py`: operation policy lookup and approval decision inputs. It
  receives the run's pinned snapshot; it does not read feature flags mid-turn.
- `tools/query/`: one `ToolGroup`, query input/result models, schema catalog
  adapter, read-only executor, and tests.
- `tools/write/`: one `ToolGroup`, discriminated operation models, operation
  registry, transaction adapter, and tests. The registry is admin-owned.
- `tools/bash/`: one thin admin declaration over the framework bash interface,
  plus admin tests for workspace identity and policy.
- `middleware/evidence.py`: admin-specific evidence authorization and durable
  metadata. It must not become a second generic tool wrapper.
- `skills/query.py`, `skills/write.py`, and `skills/bash.py`: prompt, gating,
  and mounting only. Each is built by one `build_*_skill()` function.
- `surfaces/*/bundle.py`: surface builders that mount skills. They never
  import `tools/` directly.
- `characterization/`: test-only v2 digest comparison and ledger accounting.

this layout obeys the package rule that tools own implementations, skills own
mounting, and surfaces mount skills only. See
`phoebe_admin_agent_v3/ORGANIZATION.md:87-93,148-168`.

### query

model-visible name: `query` inside the admin query skill.

input contract:

- `query`: one read-only generated-catalog query expression. The first
  implementation may accept SQL only if it uses the same parser and
  allowlist as the catalog adapter. It must reject writes, multiple
  statements, locking, and unsafe set operations.
- `description`: required human-readable reason for the read.
- `environment`: explicit `live` or `sandbox` target. No implicit `all`.
- `organization_scope`: explicit current organization, named organizations,
  or an approved cross-organization scope.
- `filters`, `fields`, `relations`, `time_window`, `group_by`, and
  `aggregates`: typed values when using catalog mode.
- `limit`: optional request, clamped to the hard bound.
- `result_destination`: `inline` or `workspace`; workspace is required for an
  explicit large-result request.

result contract:

- `columns` with stable names and declared types.
- `rows` or streamed JSONL rows for the visible part.
- `row_count`, `requested_limit`, `applied_limit`, `limit_clamped`, and
  `rows_beyond_limit`.
- `source`, `environment`, `organization_scope`, `query_id`, and an
  `evidence_ref` when the full result is outside the transcript.
- `status`: `succeeded`, `partial`, or `failed`.
- `warnings` and `caveats` are structured strings with stable codes.

the v3 query tool provides the right starting idioms: 25 default inline rows,
50 maximum inline rows, 10,000 sandbox rows, exact or unknown
`rows_beyond_limit`, streamed output, and a 40,000-token inline boundary.
See `phoebe_v3_agent/tools/query/tool.py:51-61,164-356` and
`phoebe_v3_agent/tools/query/engine/limits.py:1-32`. Admin should start with
those values, then change them only with measured evidence. The v2 admin
100,000-row and 60-second defaults are too permissive for a model-visible
primitive (`admin_database_query.py:103-116`).

bounding strategy:

1. validate the expression and scope before opening a database cursor;
2. enforce AST, join, subquery, timeout, and row limits in the executor;
3. fetch one extra row or group when possible so truncation is explicit;
4. stream directly into a bounded serializer;
5. spill once to the run workspace when the inline budget is exceeded;
6. return a small preview and evidence metadata, never a silent character
   slice.

the v3 query pipeline already fetches one extra row and reports whether more
rows exist. See `phoebe_v3_agent/tools/query/engine/pipeline.py:40-123`.

error semantics:

- `invalid_input`: malformed expression, unknown field, invalid scope, or
  unsupported query shape;
- `not_allowed`: write attempt, unauthorized organization, environment, or
  relation;
- `timeout`: statement or provider timeout;
- `resource_limit`: AST, row, byte, or workspace limit;
- `provider`: bounded provider error with no raw SQL or secret values;
- `partial`: the query ran but output was bounded or a count was unavailable.

the tool raises the framework's typed `ToolCallError` for failed calls and
returns `partial` only when the model can act on the result. This follows the
v3 query mapping of query errors to typed tool errors
(`phoebe_v3_agent/tools/query/tool.py:164-200`).

### write

model-visible name: `write` inside the admin write skill.

`write` is not arbitrary SQL and not an untyped command dispatcher. Its input
is a discriminated union:

- `operation`: stable admin capability id, such as
  `admin_note.update`, `organization_settings.update`, or
  `slack.message.send`;
- `arguments`: operation-specific frozen input model;
- `target`: explicit environment, organization, and resource id;
- `mode`: `preview` or `execute`;
- `idempotency_key`: derived from run id, operation id, target, and a caller
  supplied request key;
- `reason`: required for every mutation.

the operation registry owns input validation, authorization, transaction
boundaries, retry policy, and result normalization. It does not expose a
generic `table`, `column`, or SQL field. Each operation declares:

- approval class: `required` or `audit_only`;
- whether preview is supported;
- target and organization scope;
- idempotency behavior;
- changed-resource summary;
- rollback or compensating action;
- ledger disposition and owner.

result contract:

- `status`: `preview`, `succeeded`, `partial`, `failed`, `denied`, or
  `cancelled`;
- `operation`, `target`, `changed_count`, and stable changed ids;
- `before` and `after` only when safe and bounded;
- `approval_ref`, `audit_ref`, `evidence_ref`, warnings, and rollback hint;
- no provider stack trace and no unbounded response body.

the approval decision is made before the handler runs. A pending approval
must not open a transaction. A repeated call with the same idempotency key
returns the durable prior outcome.

### approval and the #14733 snapshot seam

use the merged v3 child approval seam as the model. The snapshot captures the
parent run, organization, mode, mounted tool names, required names,
always-allowed names, prompt context, expiry, and exact pre-approved calls.
See `phoebe_admin_agent_v3/surfaces/subagent/approval.py:104-190`.

the child reads that durable value. It never rebuilds approval from the live
parent registry. Exact grants use argument fingerprints, expiry, and a
fail-closed policy when the snapshot is absent or expired. See
`approval.py:196-245,297-349`.

for the core admin tools, extend this seam to a run-level capability snapshot:

- snapshot the mounted operation ids and versions at run creation;
- snapshot approval classes, target scope, and policy digest;
- snapshot exact grants by operation id and canonical argument fingerprint;
- pin environment and mode;
- include a digest in every write audit event;
- rehydrate only from the run snapshot, never from current tool metadata.

this keeps v2 as system of record during the dark period while allowing v3
behavior to differ after Henry approves the ledger entry. The snapshot seam
belongs in the shared harness only if it stays generic. Admin operation
classes and target rules stay in `phoebe_admin_agent_v3/core/policy.py`.

### bash

model-visible name: `run_bash` inside the admin bash skill.

input contract:

- `command`: non-empty command, bounded by bytes;
- `description`: required reason;
- `timeout_s`: bounded range;
- `presentation`: optional typed presentation request;
- `destination`: inline or workspace.

result contract:

- `exit_code`, `stdout`, `stderr`, `timed_out`, `resource_limited`, and
  `duration_ms`;
- explicit `stdout_truncated` and `stderr_truncated` flags;
- a workspace evidence ref when output spills;
- stable errors for disabled, invalid, timed out, and failed execution.

the v3 bash tool already validates command size and timeout, records the run
identity, maps executor errors, and publishes workspace registers. See
`phoebe_v3_agent/tools/bash/tool.py:43-245`.

admin should not expose `create_code_sandbox` or `destroy_sandbox` as normal
model tools. The framework owns the run-scoped sandbox. Admin policy decides
whether network, package install, file paths, and provider credentials are
allowed. The workspace identity remains
`(organization_id, mode, agent_run_id)`, as specified in the v3 organization
document.

### skill mounting

mount `query` eagerly for interactive and investigation bundles because lookup
is often the first action. Mount `write` only in bundles with an explicit
mutation policy. Mount `bash` only where the surface needs code or file
investigation. Cold specialized capabilities become skill-local adapters, not
one giant cold tool group.

`phoebe_v3_agent/skills/query.py` documents eager query mounting, and
`phoebe_v3_agent/core/agent.py:53-207` shows the v3 run path: durable history,
tool-group sync, workspace wrapping, and history-owned compaction. Admin
should follow those interfaces without importing `phoebe_v3_agent`.

## 4. compaction rethink

### current v2 shape

v2 starts compaction at 70 percent of the model context window, preserves 70
percent of the recent history, and allows a 10,000-token summary. It uses a
200,000-token fallback window and provider-specific one-million-context
compaction models. See
`libraries/python/phoebe_event_agent/phoebe_agent.py:142-165,244-299`.

the runner persists compaction through the history strategy and lease
generation. Admin adds a strict state-snapshot prompt, two validation retries,
and a callback that records consecutive failures. Three consecutive failures
become terminal. See
`phoebe_agent.py:740-813,837-897` and
`phoebe_agent.py:1294-1341`.

the runtime separately loads and persists max-output truncation state. That
state tracks a truncation streak and cumulative cost, then feeds guidance into
the next runner. See
`phoebe_event_agent/phoebe_agent.py:1258-1329` and the
`load_max_output_truncation_state` helpers.

### failure modes

- the proportional recent slice is based on estimates and can remove the
  oldest evidence that explains a later mutation;
- summaries can preserve plausible prose while losing query scope, approval
  state, or artifact identity;
- strict summary validation retries an invalid shape and then skips
  compaction, which can leave the next turn near overflow;
- tool output is bounded after execution, so the transcript can still carry
  too much data before a checkpoint runs;
- max-output state and compaction state are separate control loops;
- runtime context items add fresh state after history load, so compaction does
  not guarantee a fixed total prompt budget;
- receipt replacement uses a delayed, heuristic checkpoint rather than an
  output contract.

the checkpoint thresholds are 40,000 total tool tokens, 20,000 for a large
result, a three-turn delay, and a two-thousand-token receipt minimum. See
`phoebe_event_agent/admin_tool_checkpoints.py:1-18,226-357`.

### proposed v3 admin design

make compaction an event-boundary reducer, not a proportional text slice:

1. normalize every completed tool output into a bounded result envelope before
   it enters history;
2. persist large rows and files as evidence refs, never as raw history;
3. compact only complete turns, preserving the active turn and pending
   approvals;
4. replace old turns with a versioned admin state record containing current
   task, confirmed facts, query scopes, mutation outcomes, evidence refs,
   pending approvals, errors, and unresolved questions;
5. retain the latest bounded receipt for every referenced evidence ref;
6. validate the state record locally before asking a model to summarize;
7. if summary validation fails, persist a failure event and continue with
   receipts plus recent turns. Do not retry the same invalid shape across
   providers;
8. use one lease-fenced history callback for persistence and one generic
   max-output state record for runner backpressure.

the summary must never invent a value. Every fact needs a source turn or
evidence ref. A missing source becomes an unresolved question. A missing
approval snapshot fails closed.

the shared harness may own the event-boundary callback, lease fencing, and
generic max-output state. The admin state schema and validation remain
admin-side unless a second agent needs the same generic typed state model.

## 5. artifacts and receipts rethink

### current v2 shape

v2 has at least four overlapping output paths:

- `AdminToolResult` inline output with caps and truncation markers;
- durable full-result artifacts in database or object storage;
- sandbox receipts with file path, byte count, digest, and preview;
- compacted-result previews and `read_tool_result_by_id` readback.

the controlled wrapper applies a universal 16 KiB visible cap, can spill before
applying caps, and has a preserve-full escape. It also records raw and visible
byte sizes. See
`phoebe_event_agent/admin_tool_controls.py:898-1029,1157-1207` and
`admin_tool_controls.py:1265-1319`.

readback is tied to the active run, mode, organization, conversation, and
tool-call id. It can hydrate a full artifact and verify integrity, or fall
back to an artifact UUID. See
`phoebe_event_agent/admin_tool_readback.py:118-214`.

the checkpoint receipt contains a tool call id, tool name, source item id,
load command, digest, bytes, and hydration state. It replaces old output only
after the 40,000-token or delayed-large-result thresholds. See
`phoebe_event_agent/admin_tool_checkpoints.py:40-154,265-357`.

### problems

- one result can have a visible result, a full artifact, a sandbox copy, and a
  receipt with different identifiers;
- `preserve_full` bypasses the normal cap contract;
- load commands are model-facing strings, not typed references;
- artifact lookup is coupled to conversation layout and legacy item shapes;
- inline and streamed results use different bounding paths;
- the universal byte cap is too small for useful admin investigation and too
  late to protect the model context;
- preview placeholders can hide whether the full data is authorized and
  available.

### proposed v3 design

define one admin-owned `EvidenceRef`:

- `ref`: opaque run-scoped identifier;
- `kind`: `workspace`, `durable_artifact`, or `presentation`;
- `format`: JSON, JSONL, CSV, text, image, or provider-specific typed format;
- `producer`: tool name, call id, run id, and operation version;
- `scope`: organization, mode, and run;
- `bytes`, `sha256`, `row_count`, and schema hint;
- `preview`: bounded head only;
- `read_policy`: stream, row range, byte range, or presentation only;
- `expires_at` for transient workspace refs.

normal tool results contain a small preview plus `EvidenceRef`. A single
typed `read_evidence` capability may load a bounded slice by ref and range.
It must not accept a free-form load command or a tool-call id without a ref.

storage rule:

- use workspace for transient run-local query, bash, and file output;
- use durable artifacts only for user-visible presentations, explicit exports,
  or evidence that must survive the run;
- keep audit events separate from evidence bytes;
- authorize every read by organization, mode, run, and ref kind;
- verify sha256 and byte count on hydration;
- never put full artifact payloads into compaction summaries.

the v3 harness workspace spool is a useful analogue: it streams into a
bounded file, records row count and metadata, creates a deterministic
workspace ref, and uses a writer lease. See
`phoebe_v3_agent/middleware/workspace.py:77-155,271-357,505-561`. Admin should
use the same interface contract only after moving any generic part into the
shared framework. It must not import or copy the v3 agent package.

## 6. ladder impact

### old ladder changes

- old PRs 2-3 that extract v2 contracts, wrappers, or artifact implementations
  die as ports. Keep only a generic framework seam if its interface has a
  second consumer and it carries no admin fields.
- old approval and persistence work survives only as the v3 run-level snapshot
  and evidence contract. The #14733 snapshot is the starting seam, not a
  reason to retain v2 wrappers.
- old PR 6, the governed admin tree, remains. The merged scaffold already
  supplies the tree, organization rules, and ledger. New implementation must
  fit that tree.
- old PR 7, adapter/core policy dark build, becomes the core context, result,
  policy, and snapshot contract. It must not adapt v2 tool implementations.
- old PR 8, tools and skills through one mount, becomes the query/write/bash
  family implementation sequence.
- old PR 9, MCP, is dropped from the critical path. Add it only after a real
  provider use case gets an allowlist and ledger owner.
- old PR 10, dark surface bundles, remains after core tools and evidence are
  stable.
- old PRs 11-14, interactive, slack, quality, and scheduled surfaces, remain
  as separate dark builds and gated cutovers.
- old PR 15, subagents, changes to one delegate contract plus the immutable
  child snapshot. Parallel investigation and court workflows become skills.
- old PRs 16-17, drain and delete, remain last. They require end-to-end v3
  validation and per-surface gates.

the old plan's PR labels and order are in the ticket description. The revised
sequence below replaces behavior-preserving extraction with independent
redesign.

### revised bounded PR sequence

1. **ledger and characterization accounting**
   reconcile 190 ledger entries with the 188-name digest, including the two
   digest-only slack names. Add no runtime code.
2. **admin v3 core contracts**
   add `AdminRunContext`, result/error envelopes, evidence refs, operation
   ids, and the package-local layering tests. No v2 import and no composition
   root.
3. **run approval snapshot**
   generalize the #14733 immutable snapshot for admin operation ids, target
   scope, policy digest, and exact grants. Add fail-closed tests. Keep this
   dark.
4. **query family**
   implement one catalog-backed `query`, schema discovery as a skill-local
   helper, bounded execution, structured partial results, and contract tests.
   Do not port the five v2 database definitions.
5. **write family**
   implement the typed operation registry, preview/execute modes,
   idempotency, transaction boundary, approval handoff, and a small first set
   of operations. Require Henry's ledger approval for each user-visible
   redesign.
6. **bash and workspace evidence**
   expose `run_bash`, wire the run-scoped workspace, and add bounded stdout,
   stderr, and file evidence. Split a shared framework change if required.
7. **compaction and readback**
   add the event-boundary reducer, state record, evidence hydration, and
   generic max-output backpressure. Test lease loss, invalid summaries, and
   missing refs.
8. **interactive dark bundle**
   mount query, selected writes, bash, and evidence for one internal admin
   surface. Compare behavior and latency against v2.
9. **slack dark bundle and cutover**
   add slack read/write skills, per-operation approvals, and one gated surface
   cutover. Keep v2 as fallback.
10. **scheduled and automation bundles**
    add explicit run policies and no implicit user approvals. Keep each
    schedule or automation profile independently gated.
11. **delegate and subagent bundle**
    add child creation, immutable snapshot propagation, evidence scope, and
    named investigation skills. Add court and probe only if current use proves
    them.
12. **specialized domain families**
    port notes, pages, settings, calls, quality, codebase, and external
    research as separate design-and-build PRs. Each PR may merge, redesign, or
    descoped names in the ledger.
13. **comparative gate and drain**
    require all ledger entries to have a disposition, all expected divergence
    to be explained, and all selected surfaces to pass end-to-end evaluation.
    Then drain old runs and remove v2 wiring and compatibility code.

each PR has one contract and one review surface. None imports
`phoebe_v3_agent`. No PR wires the admin v3 package before the relevant
surface gate.

## 7. open decisions for henry

1. **query input shape.** recommendation: use the generated catalog expression
   as the public contract, not arbitrary SQL. This makes organization scope,
   relation paths, and limits typed. The tradeoff is less flexibility for rare
   investigations; add a separately approved read-only SQL adapter only if
   measured demand proves it.
2. **write shape.** recommendation: expose one `write` name with a
   discriminated operation union. This reduces model-visible tools and keeps
   each handler typed. The tradeoff is a large schema; skill-local operation
   packs and strict operation ids control that size.
3. **first write operations.** recommendation: start with notes, settings, and
   one Slack message operation. Defer pages, routing, feature flags, sales,
   and external workflow mutations until the approval and evidence contracts
   pass. The tradeoff is slower feature coverage.
4. **approval classes.** recommendation: require approval for external,
   customer-visible, permission, routing, deletion, and scheduled mutations.
   Use audit-only for drafts, internal analysis records, and run-local
   artifacts. The tradeoff is more prompts for borderline operations. Record
   each decision in the ledger.
5. **query bounds.** recommendation: begin with 50 inline rows, 10,000
   workspace rows, 10 MiB hard serialized output, and a five-second statement
   timeout. The tradeoff is that large reports need explicit workspace reads.
6. **workspace retention.** recommendation: transient evidence expires with
   the run; durable evidence requires explicit export or presentation intent.
   The tradeoff is less convenient late readback, with lower storage and
   privacy risk.
7. **compaction failure.** recommendation: preserve receipts and recent turns
   after one invalid summary, record the failure, and do not retry the same
   invalid schema. The tradeoff is shorter context after a provider failure.
8. **dynamic MCP.** recommendation: drop the generic dynamic surface from the
   first ladder. Reintroduce a named provider adapter only with an allowlist,
   stable schema, owner, and ledger entry. The tradeoff is delayed provider
   coverage.
9. **subagent scope.** recommendation: build only child run plus immutable
   approval snapshot first. Add court, probe, and parallel investigation as
   skills after one real workflow needs them. The tradeoff is fewer early
   automation features.
10. **surface order.** recommendation: validate interactive first, then Slack,
    then scheduled and automation, then subagent. This gives the core tools a
    narrow user-visible test before background execution. The tradeoff is that
    scheduled users wait longer.
11. **ledger approval rule.** recommendation: require explicit Henry approval
    for every mutation-class or user-visible redesign, as the v3 ledger
    requires. Do not treat digest accounting as behavior parity.
12. **old runs.** recommendation: keep v2 as system of record and use drain
    policy for old runs. Do not backfill or lazily migrate history. The tradeoff
    is duplicate runtime support during the validation window.

## 8. general-purpose candidates

these candidates need explicit sign-off before moving below the admin package.
They are candidates only when they have no admin-specific fields or policy.

- immutable mounted-tool and exact-call approval snapshots. The v3 subagent
  snapshot already has a generic shape: mounted names, required names,
  always-allow names, expiry, prompt context, and argument fingerprints
  (`phoebe_admin_agent_v3/surfaces/subagent/approval.py:104-245`). Move only
  the generic snapshot and rehydration interface to the shared harness.
- bounded streamed serialization. The v3 workspace spool already avoids
  allocating beyond a hard byte cap and records row count and metadata
  (`phoebe_v3_agent/middleware/workspace.py:271-339`). A shared interface is
  useful for query, bash, and future agents.
- run-scoped workspace output and typed evidence refs. The shared part is
  identity, writer leasing, paths, digest, byte count, and bounded hydration.
  Admin authorization and artifact retention stay admin-side.
- history-owned, lease-fenced compaction callbacks. The v2 runner already
  passes persistence through history and lease generation
  (`phoebe_event_agent/phoebe_agent.py:740-758`). The shared harness can own
  this lifecycle while admin owns its state schema.
- generic max-output backpressure state. A framework record for truncation
  streak, cumulative cost, and next-turn guidance may serve all agents. Admin
  policy and terminal thresholds must not enter the framework.
- typed tool error categories and bounded provider-error sanitization. The
  shared interface may define invalid input, denied, timeout, resource limit,
  provider, and cancelled outcomes. Admin-specific redaction rules stay in the
  admin adapter.

do not move these items solely to reduce the admin diff. The v3 organization
rule says a framework type must not accumulate admin fields or flags, and a
shared helper needs a second real consumer. See
`phoebe_admin_agent_v3/ORGANIZATION.md:70-85,197-217`.
