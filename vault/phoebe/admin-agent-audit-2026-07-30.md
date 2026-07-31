---
type: reference
tags: [phoebe, admin-agent, audit]
created: 2026-07-30
updated: 2026-07-30
---

# Admin agent audit 2026-07-30

# Phoebe Internal Admin Agent Audit

Pinned source: `origin/main` commit `7a3a01e1e25c1badacdc4f31ab599d6e8ad3e9a5`.

Method: fetched `origin/main`, exported the pinned tree to `/tmp`, and read the
enforcing code. I did not change the checkout. I did not run tests or contact
production services, as required.

## Executive summary

The static admin runtime has 182 possible tools:

- 171 tools come from `ADMIN_AGENT_TOOL_REGISTRY`.
- `read_tool_result_by_id` is the registry-adjacent base reader.
- Eight legacy settings and phone tools mount directly at top level.
- Two Slack writers mount only in Slack-admin or scheduled-quality modes.
- Runtime MCP discovery can add more tools. It is disabled by default and
  depends on environment configuration, so no fixed source-only count exists.
- Framework tools such as `load_skill`, `view_skills`, and provider tool search
  are not admin data tools. They are outside this inventory.

Of the 182 static tools, 127 are read contracts and 55 are mutation contracts or
direct mutation tools. Thirteen mutations require human tool approval. Forty
registry mutations use audit-only execution. Two conditional Slack writers use
run-specific controls without human approval.

The largest risk is runtime exposure. Scheduled top-level runs retain all 40
general audit-only mutation tools. A scheduled quality run can also receive its
purpose-built Slack writer. Admin subagents retain 30 audit-only mutation tools.
These are code-level capabilities, not only prompt text.

The SELECT-only database mechanism is also incomplete. Each query uses a
read-only transaction, and the bootstrap role has the right intended grants.
However, DSN validation checks only `INSERT` on `app.organizations`. A role with
another write privilege can pass the advertised write-capable-DSN rejection.

The standard registry has strong controls. Each input declares audit scope.
Controlled calls reserve a durable start event. Results use strict schemas.
Successful calls get a terminal audit event. A universal 16 KiB model-visible
cap spills large results to artifacts. These controls do not cover the direct
settings, phone, or conditional Slack groups.

The common admin API router requires platform-admin authentication. The
scratchpad router establishes a mode from the request header. The feature-flag
write paths from PR #12855 enforce the new eligibility allowlist in both API and
agent writes. The runtime does not repeat that eligibility check.

PR #12857 correctly made scratchpad search scope errors actionable. It also
treats a missing Turbopuffer namespace as an empty result in the agent tool.
The admin API search path still treats the same 404 as an outage.

Finding count: **5 high, 11 medium, 4 low**.

## Gate model and mutation inventory

The registry maps only `PHOEBE_PRODUCTION_MUTATION` and
`LIVE_CUSTOMER_SIDE_EFFECT` to human approval
(`libraries/python/phoebe_event_agent/admin_tool_contracts.py:181`).
Every other mutation policy is audit-only. Registry validation enforces that
manifest choice (`libraries/python/phoebe_admin_agent/admin_tools.py:400`).

The 13 human-approval tools are:

- `activate_admin_page_version`
- `refresh_admin_page_data`
- `configure_admin_page_refresh_schedule`
- `manage_admin_agent_automation`
- `invite_admin_organization_user`
- `create_admin_sales_agreement`
- `upsert_org_territory`
- `update_admin_organization_feature_flags`
- `update_admin_organization_settings`
- `add_admin_phone_forwarding`
- `update_admin_phone_forwarding`
- `add_admin_call_routing_destination`
- `update_admin_call_routing_destination`

The 40 audit-only registry mutation tools are:

- `record_run_turn`, `replay_recorded_turn`, `report_missing_tool`
- `set_call_recording_tags`
- `create_admin_page_version_from_sandbox`
- `create_code_sandbox`, `load_artifact_into_sandbox`,
  `emit_admin_image_artifact`, `run_sandbox_command`, `run_code_in_sandbox`,
  `read_sandbox_file`, `write_sandbox_file`, `export_sandbox_diff`,
  `destroy_sandbox`, `list_sandboxes`, `emit_admin_file_artifact`
- `propose_skill_update`
- `run_agent_court_judge`, `queue_agent_court_justice`,
  `preview_head_justice_digest`
- `create_or_resolve_general_agent_probe_org`, `run_general_agent_probe`
- `post_admin_github_pr_review`
- `write_admin_note`, `update_admin_note`, `retire_admin_note`,
  `confirm_admin_note`
- `apply_linear_workflow_mutation`, `apply_admin_notion_write`
- `post_account_health_threads_to_slack`, `post_to_slack_channel`,
  `reply_to_slack_thread`, `upload_file_to_slack`,
  `schedule_slack_message`, `update_slack_message`,
  `delete_slack_message`
- `request_orchard_code_change_task`
- `create_sub_agent`, `create_sub_agents`, `message_sub_agent`

The two conditional direct mutations are `post_to_slack_thread` and
`post_daily_tool_quality_review_to_slack`. The first uses a per-turn duplicate
claim. The second requires a scheduled, non-dry-run context and uses a stable
Slack idempotency key.

## Complete tool inventory

`contract` and `effect scope` come from the enforcing
`AdminToolDefinition`, not docstrings. Registry tools also require each input
schema to implement `audit_scope()`. `scheduled top level` describes runtime
mounting, not whether the model sees the schema before loading a cold pack.

| tool | contract | effect scope | organization/data scope | gate | scheduled top level | admin subagent | enforcing source |
|---|---|---|---|---|---|---|---|
| `inspect_admin_tool_registry_fixture` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_registry_fixture.py:78` |
| `record_run_turn` | mutation | `sandbox_probe_fixture` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_recorded_turn.py:1446` |
| `replay_recorded_turn` | mutation | `sandbox_probe_fixture` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_recorded_turn.py:1481` |
| `emit_admin_chart_artifact` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_chart_artifact_tool.py:72` |
| `emit_admin_csv_artifact` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_csv_artifact_tool.py:120` |
| `report_missing_tool` | mutation | `admin_investigation_metadata` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_missing_tool_reports.py:148` |
| `resolve_admin_investigation_context` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_investigation_context.py:3762` |
| `fetch_live_ehr_record` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_live_ehr_record.py:165` |
| `search_call_recording_index` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_call_transcripts.py:598` |
| `read_call_recording_transcript` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_call_transcripts.py:643` |
| `search_call_recording_transcript` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_call_transcripts.py:676` |
| `set_call_recording_tags` | mutation | `call_recording_tags` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_call_transcripts.py:711` |
| `get_admin_account_health_metrics` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_account_health_read.py:152` |
| `get_admin_account_health_rollups` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_account_health_rollup.py:687` |
| `list_admin_pages` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:175` |
| `read_admin_page` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:198` |
| `create_admin_page_version_from_sandbox` | mutation | `admin_page_versions` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:232` |
| `activate_admin_page_version` | mutation | `admin_page_activation` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | approval snapshot only | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:319` |
| `refresh_admin_page_data` | mutation | `admin_page_refresh` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | approval snapshot only | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:379` |
| `configure_admin_page_refresh_schedule` | mutation | `admin_page_refresh_schedule` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | approval snapshot only | `libraries/python/phoebe_admin_agent/tools/admin_pages.py:448` |
| `create_code_sandbox` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:428` |
| `load_artifact_into_sandbox` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:503` |
| `emit_admin_image_artifact` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:702` |
| `run_sandbox_command` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:858` |
| `run_code_in_sandbox` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:915` |
| `read_sandbox_file` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:1005` |
| `write_sandbox_file` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:1134` |
| `export_sandbox_diff` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:1217` |
| `destroy_sandbox` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:1292` |
| `list_sandboxes` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_code_sandbox.py:1882` |
| `emit_admin_file_artifact` | mutation | `code_sandbox` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_file_artifact.py:188` |
| `inspect_codebase_wiki` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:1901` |
| `read_codebase_source_range` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2395` |
| `search_codebase_symbols` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2486` |
| `outline_codebase_file` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2563` |
| `search_codebase_source` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2639` |
| `find_codebase_references` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2716` |
| `search_codebase_knowledge` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2783` |
| `inspect_codebase_map` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2858` |
| `inspect_codebase_relationships` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2921` |
| `localize_codebase_issue` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:2999` |
| `localize_runtime_evidence_to_code` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:3086` |
| `investigate_codebase` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:3187` |
| `review_codebase_evidence` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_codebase_wiki.py:3305` |
| `check_admin_core_api_health` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:446` |
| `resolve_core_account_owner` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:512` |
| `get_core_account_book` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:544` |
| `get_core_account_dossier` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:586` |
| `get_core_funnel_snapshot` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:615` |
| `list_admin_core_accounts` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:643` |
| `get_admin_core_account_context` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:699` |
| `list_admin_core_events` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:733` |
| `search_admin_core_tam_accounts` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:780` |
| `get_admin_core_tam_account` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:827` |
| `get_org_context_index` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:404` |
| `load_org_context_summary` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_core_api.py:425` |
| `list_core_account_emails` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_core_email.py:134` |
| `get_core_email_message` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_core_email.py:226` |
| `get_core_email_thread` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_core_email.py:267` |
| `get_core_intercom_conversation` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_core_intercom.py:94` |
| `get_admin_crm_context` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_crm_context.py:104` |
| `diagnose_kickoff_readiness` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_kickoff_readiness.py:91` |
| `get_admin_meeting_context` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_meeting_context.py:103` |
| `get_onboarding_checklist_state` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_onboarding_checklist.py:107` |
| `compare_agent_runs` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_run_comparison.py:1292` |
| `inspect_agent_run_trace` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_run_trace.py:3928` |
| `propose_skill_update` | mutation | `admin_skill_overrides` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/tools/admin_agent_skill_overrides.py:188` |
| `list_agent_court_cases` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_court_workflows.py:566` |
| `get_agent_court_case` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_court_workflows.py:734` |
| `run_agent_court_judge` | mutation | `agent_court_analysis` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_court_workflows.py:838` |
| `queue_agent_court_justice` | mutation | `agent_court_analysis` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_court_workflows.py:933` |
| `preview_head_justice_digest` | mutation | `agent_court_analysis` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_court_workflows.py:995` |
| `search_schema` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_database_query.py:2767` |
| `describe_table` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_database_query.py:2790` |
| `list_admin_database_queries` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_database_query.py:2588` |
| `run_admin_database_query` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_database_query.py:2640` |
| `query_admin_database` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_database_query.py:2718` |
| `inspect_admin_organization_membership` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_organization_membership.py:165` |
| `invite_admin_organization_user` | mutation | `organization_membership` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/admin_organization_membership.py:241` |
| `list_admin_sales_agreement_templates` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_sales_agreements.py:323` |
| `create_admin_sales_agreement` | mutation | `sales_agreement` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/admin_sales_agreements.py:369` |
| `list_org_territories` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_organization_territories.py:414` |
| `resolve_org_territory` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_organization_territories.py:469` |
| `resolve_org_territories` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_organization_territories.py:535` |
| `upsert_org_territory` | mutation | `organization_territories` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/admin_organization_territories.py:592` |
| `get_admin_organization_feature_flags` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_org_feature_flags.py:195` |
| `update_admin_organization_feature_flags` | mutation | `organization_feature_flags` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/admin_org_feature_flags.py:287` |
| `survey_org_feature_adoption` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_org_feature_adoption.py:307` |
| `search_feature_catalog` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_feature_catalog.py:155` |
| `get_org_ehr_state` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_org_ehr_state.py:681` |
| `classify_organization_shifts` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_shift_classification.py:477` |
| `search_admin_linear_issues` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:117` |
| `inspect_admin_linear_issue` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:169` |
| `inspect_admin_linear_document` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:207` |
| `list_admin_github_repositories` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:236` |
| `search_admin_github_pull_requests` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:269` |
| `inspect_admin_github_pull_request` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:308` |
| `get_admin_github_file` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:445` |
| `compare_admin_github_refs` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:507` |
| `list_admin_github_commits` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:552` |
| `inspect_admin_github_commit` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:599` |
| `list_admin_github_branches` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:633` |
| `list_admin_github_tags` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:667` |
| `list_admin_github_workflow_runs` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:704` |
| `inspect_admin_github_workflow_run` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:757` |
| `search_admin_notion` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:796` |
| `inspect_admin_notion_page` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:842` |
| `list_admin_calendars` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:893` |
| `list_admin_calendar_events` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:936` |
| `inspect_admin_calendar_event` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_external_api_tools.py:988` |
| `get_current_incident_io_on_call` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_incident_io.py:51` |
| `compare_admin_agent_source_health` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_operational_controls.py:106` |
| `validate_admin_capability_readiness` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_operational_controls.py:141` |
| `inspect_admin_feature_flag_rollout` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_operational_controls.py:234` |
| `preview_admin_feature_flag_updates` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_operational_controls.py:281` |
| `create_or_resolve_general_agent_probe_org` | mutation | `sandbox_probe_fixture` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_general_agent_probe_seeded_org.py:120` |
| `run_general_agent_probe` | mutation | `admin_investigation_metadata` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_general_agent_probe.py:209` |
| `post_admin_github_pr_review` | mutation | `github_pull_request_review` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_github_pr_review.py:257` |
| `search_run_data` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_run_data.py:541` |
| `read_run_data` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_run_data.py:668` |
| `slice_json` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_run_data.py:863` |
| `diff_run_data` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_run_data.py:950` |
| `investigate_posthog_analytics` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_posthog_tools.py:665` |
| `get_admin_posthog_account_health_users` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_posthog_account_health.py:595` |
| `investigate_datadog_alert` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_datadog_investigation.py:207` |
| `investigate_logfire_records` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_logfire_investigation.py:80` |
| `investigate_braintrust_evals` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_braintrust_investigation.py:67` |
| `preview_admin_notion_write` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_notion_workflows.py:292` |
| `apply_admin_notion_write` | mutation | `notion_api_workflow` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_notion_workflows.py:366` |
| `investigate_token_alert` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_token_alert_workflow.py:919` |
| `sweep_agent_health` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_health_sweep_workflow.py:99` |
| `review_admin_tool_quality` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_tool_quality_review.py:772` |
| `search_notes` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_agent_notes_search.py:253` |
| `write_admin_note` | mutation | `admin_agent_notes` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:920` |
| `update_admin_note` | mutation | `admin_agent_notes` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:1004` |
| `retire_admin_note` | mutation | `admin_agent_notes` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:1086` |
| `confirm_admin_note` | mutation | `admin_agent_notes` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:1138` |
| `search_admin_notes` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:777` |
| `read_admin_note` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:835` |
| `read_admin_note_revisions` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:861` |
| `search_index` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_index_search.py:264` |
| `triage_agent_run_observability` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_agent_observability_triage_workflow.py:106` |
| `run_admin_investigation_playbook` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_investigation_playbook_library.py:428` |
| `inspect_admin_integration_migration` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_integration_migration_inventory.py:110` |
| `map_admin_work_item_to_codebase` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_knowledge_workflows.py:280` |
| `search_saved_admin_investigations` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_saved_investigation_search.py:284` |
| `apply_linear_workflow_mutation` | mutation | `linear_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_linear_workflows.py:351` |
| `post_account_health_threads_to_slack` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_slack/slack_agent_tools.py:1305` |
| `post_to_slack_channel` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:725` |
| `reply_to_slack_thread` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:823` |
| `upload_file_to_slack` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:860` |
| `schedule_slack_message` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1217` |
| `update_slack_message` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1295` |
| `delete_slack_message` | mutation | `slack_workspace` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1383` |
| `list_slack_channels` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1462` |
| `get_slack_channel_info` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1499` |
| `search_slack_messages` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1535` |
| `get_slack_thread` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1618` |
| `lookup_slack_user` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:1678` |
| `request_orchard_code_change_task` | mutation | `orchard_draft_pr_task` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | not mounted | `libraries/python/phoebe_admin_agent/admin_orchard_code_change.py:312` |
| `manage_admin_agent_automation` | mutation | `agent_automations` | input `audit_scope()`; cross-org admin capability | human approval | mounted; approval | approval snapshot only | `libraries/python/phoebe_admin_agent/admin_agent_automations.py:596` |
| `summarize_admin_work_plan` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/tools/admin_work_planning.py:60` |
| `create_sub_agent` | mutation | `admin_investigation_metadata` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_event_agent/subagent_tools.py:305` |
| `create_sub_agents` | mutation | `admin_investigation_metadata` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_event_agent/subagent_tools.py:456` |
| `message_sub_agent` | mutation | `admin_investigation_metadata` | input `audit_scope()`; cross-org admin capability | audit-only | mounted; no approval | mounted | `libraries/python/phoebe_event_agent/subagent_tools.py:1636` |
| `list_sub_agents` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_event_agent/subagent_tools.py:2294` |
| `dump_sub_agent` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_event_agent/subagent_tools.py:2401` |
| `run_admin_parallel_investigation` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_event_agent/admin_parallel_investigation.py:458` |
| `investigate_outreach_agent_run` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_outreach_investigation_workflow.py:287` |
| `inspect_outreach_start_actions` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_outreach_start_actions.py:318` |
| `reconcile_outreach_suggestions` | read | `none` | input `audit_scope()`; cross-org admin capability | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_outreach_reconciliation_workflow.py:462` |
| `read_tool_result_by_id` | read | `none` | same run, organization, conversation, and mode | none | mounted | mounted | `libraries/python/phoebe_admin_agent/admin_tools.py:502` |
| `find_admin_settings_location` | read | `none` | static source map | none | mounted | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_settings_location.py:47` |
| `get_admin_organization_settings` | read | `none` | current organization and runtime mode | none | mounted | not mounted | `libraries/python/phoebe_admin_agent/admin_org_settings.py:111` |
| `update_admin_organization_settings` | mutation | `organization settings` | current organization; missing mode defaults live | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/admin_org_settings.py:181` |
| `get_admin_phone_routing` | read | `none` | current organization and runtime mode | none | mounted | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:88` |
| `add_admin_phone_forwarding` | mutation | `phone routing` | current organization; missing mode defaults live | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:183` |
| `update_admin_phone_forwarding` | mutation | `phone routing` | current organization; missing mode defaults live | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:466` |
| `add_admin_call_routing_destination` | mutation | `phone routing` | current organization; missing mode defaults live | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:616` |
| `update_admin_call_routing_destination` | mutation | `phone routing` | current organization; missing mode defaults live | human approval | mounted; approval | not mounted | `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:814` |
| `post_to_slack_thread` | mutation | `Slack source thread` | run-bound organization, mode, and source thread | dedup claim; no human approval | Slack-admin runs only | not mounted | `libraries/python/phoebe_slack/slack_agent_tools.py:1119` |
| `post_daily_tool_quality_review_to_slack` | mutation | `Slack workspace` | server-pinned channel for scheduled quality runs | scheduled capability plus idempotency key | quality automation only | not mounted | `libraries/python/phoebe_admin_agent/admin_slack_workflows.py:754` |

## High-severity findings

### H1. Scheduled runs retain the full general mutation surface

**[high] `libraries/python/phoebe_event_agent/runtime_assembly.py:4416`**

The runtime builds the complete registry tool and skill surface before it sets
`is_scheduled` in tool context. It does not filter definitions by run kind.
Scheduled automation handling adds prompt instructions only
(`libraries/python/phoebe_event_agent/runtime_assembly.py:4572`).

The top-level approval config gates only the 13 human-approval names
(`libraries/python/phoebe_event_agent/runtime_assembly.py:2858`). Therefore, a
scheduled run can execute all 40 general audit-only mutations. Examples include
posting GitHub reviews, running Agent Court actions, writing scratchpad notes,
changing skill overrides, creating subagents, and operating code sandboxes.
When configured, the quality-review Slack writer is added as a 41st intended
unattended writer.

This violates the requested rule that general mutation tools stay unreachable
from unattended runtimes. A prompt instruction cannot enforce capability
isolation.

**Recommended fix:** Build a separate scheduled registry before skill
construction. Start with read-only definitions. Add only exact automation
output tools through a server-owned allowlist. For the quality review, allow
only `review_admin_tool_quality`, artifact persistence, and the pinned Slack
writer. Reject any scheduled bundle whose visible or loadable tool union
contains another mutation definition.

### H2. Admin subagents retain 30 audit-only mutation tools

**[high] `libraries/python/phoebe_event_agent/runtime_assembly.py:2878`**

Subagent filtering uses a hand-written exclusion list. It removes organization
flags, membership, territories, sales agreements, Slack writes, Linear,
Notion, and Orchard. It does not derive the boundary from `read_only`.

Thirty audit-only mutations remain loadable. They include
`post_admin_github_pr_review`, four scratchpad writers, three Agent Court
actions, ten code-sandbox contracts, three subagent-control writers, and probe
or fixture writers. The child can call `create_sub_agent`; its context maps that
operation back to the original parent run
(`libraries/python/phoebe_event_agent/subagent_tools.py:3466`).

The runtime test confirms that the subagent orchestration pack contains
`create_sub_agent` and only checks the hand-maintained excluded set
(`libraries/python/phoebe_admin_agent/runtime_assembly_admin_test.py:1997`).

**Recommended fix:** Mount only definitions with `read_only=True` in admin
subagents. Do this before hot/cold pack construction. If a future task needs a
write, use an exact server-created capability snapshot. Include the tool name,
argument digest, scope, mode, expiry, and one-use call identifier. Do not allow
child runs to create or message sibling tasks by default.

### H3. DSN validation does not reject all write-capable roles

**[high] `libraries/python/admin_agent_sources/routing.py:319`**

`verify_read_only_postgres_connection` checks superuser status, RLS bypass, and
only `INSERT` on `app.organizations`. The code comment says it verifies no write
grant on app tables, but it does not check:

- `UPDATE`, `DELETE`, or `TRUNCATE` on `app.organizations`
- any write privilege on another app table
- sequence mutation privileges
- `CREATE` on the schema
- function execution that can mutate through `SECURITY DEFINER`
- inherited or grant-option privileges

A role with one of these capabilities can pass DSN validation. The local route
can also fall back from `ADMIN_AGENT_LOCAL_POSTGRES_URL` to `DATABASE_URL`
(`libraries/python/admin_agent_sources/routing.py:1232`).

Each query still opens an explicit read-only transaction
(`libraries/python/admin_agent_sources/routing.py:546`). The bootstrap role is
also configured correctly as `NOBYPASSRLS`, read-only, and SELECT-only
(`database/bootstrap.sql:194`). These are useful safeguards, but they do not
make the claimed DSN rejection true.

**Recommended fix:** Verify effective privileges across the full `app` schema.
Reject table writes, sequence writes, schema creation, and unsafe executable
functions. Prefer an exact accepted role identity plus an effective-grant
audit. Keep the explicit read-only transaction. Add one exact test for every
rejected privilege class.

### H4. Ten direct tools bypass the registry control contract

**[high] `libraries/python/phoebe_event_agent/runtime_assembly.py:4419`**

The runtime mounts settings and phone groups outside
`ADMIN_AGENT_TOOL_REGISTRY`. It conditionally adds two more direct Slack
groups. These tools do not receive the standard:

- `AdminToolInput` scope declaration
- strict `AdminToolResult` validation
- durable reserved start event
- terminal success, failure, denial, and cancellation audit
- standard result cap and artifact spill
- standard model-safe retry envelope
- manifest validation against side-effect policy

Five direct settings and phone tools mutate production state. Name-based
approval protects them today. That protection depends on keeping three
separate name sets synchronized.

The direct Slack final-answer tool also performs an external write without a
standard admin tool event. It has a duplicate claim, but its error dictionaries
are ordinary successful tool returns.

**Recommended fix:** Convert all direct groups into
`AdminToolDefinition` entries. Use strict per-tool input and output schemas.
Declare their mode and organization scope in `audit_scope()`. Derive approval
names from the definitions. Keep Slack mode checks and duplicate claims inside
the controlled function.

### H5. Direct production mutations commit before the action audit

**[high] `libraries/python/phoebe_admin_agent/admin_org_settings.py:266`**

The settings transaction ends before `emit_admin_action_audit` runs at line
333. The general settings path has the same order at lines 430 and 556.

All four phone-routing writers also close their database transaction before the
action audit:

- forwarding create:
  `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:344`
- forwarding update:
  `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:516`
- destination create:
  `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:712`
- destination update:
  `libraries/python/phoebe_admin_agent/tools/admin_phone_routing.py:867`

The shared emitter explicitly supports a caller transaction so the audit can
commit atomically (`libraries/python/admin_action_audit/__init__.py:86`).
These callers do not pass it. An audit database failure can therefore leave a
committed state change without its admin action row. Settings emit some domain
events in the write transaction. The phone writers do not have an equivalent
atomic admin action record.

**Recommended fix:** Call `emit_admin_action_audit(...,
db_session=db_session)` inside each state transaction. Build the final audit
payload before commit. Use an outbox for after-commit external sync. Add a
rollback test that forces audit insertion to fail and asserts the state did not
change.

## Medium-severity findings

### M1. Database queries default to cross-mode and unbounded reads

**[medium] `libraries/python/phoebe_admin_agent/admin_database_query.py:354`**

`mode` defaults to `all`. `limit` defaults to `None`, which means all matching
rows. For a mode-aware table, `all` adds no mode predicate
(`libraries/python/phoebe_admin_agent/admin_database_query.py:1524`).

The query fetches all raw rows before result caps apply
(`libraries/python/phoebe_admin_agent/admin_database_query.py:2250`). It warns
about mixed modes only when the caller selected the `mode` field and visible
rows contain both modes. An omitted `mode` field hides the mixture.

This allows an ordinary live-run query to combine live and sandbox state. A
ten-second timeout and EXPLAIN cost limit reduce risk, but they do not bound
returned row memory.

**Recommended fix:** Require an explicit mode. Default to the trusted run mode
only if omission must remain compatible. Make `all` an explicit cross-mode
opt-in in the audit scope. Enforce a hard database `LIMIT` and cursor
pagination. Spill full exports through a separate artifact path.

### M2. Scratchpad ID operations bypass explicit cross-scope policy

**[medium] `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:249`**

Read, revision, write, update, retire, and confirm inputs report
`current_organization` through `_scratchpad_audit_scope`. However,
`write_admin_note` accepts any organization `scope_id`
(`libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:920`).
ID-based reads and mutations select only by note ID.

The API list route requires explicit cross-scope opt-in
(`services/api/routes/admin/agent_scratchpad.py:507`). The detail and revision
routes select by ID with no equivalent scope check
(`services/api/routes/admin/agent_scratchpad.py:805`).

The test suite explicitly creates a note for another organization without a
cross-scope input
(`libraries/python/phoebe_admin_agent/admin_agent_scratchpad_test.py:476`).
The resulting generic audit scope says current organization, not the target.

**Recommended fix:** Add `cross_scope` to write and ID-based inputs. Resolve a
note's true scope before execution, then call
`record_current_admin_tool_audit_scope`. Reject foreign organization IDs unless
`cross_scope=true`. Apply the same rule to API detail, revisions, and by-run
lookups.

### M3. Scratchpad updates allow stale overwrite and implicit ref deletion

**[medium] `libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:297`**

`UpdateAdminNoteInput` has no expected version. It also defaults
`evidence_refs` to an empty list. The function turns omission into `[]`, locks
the latest row, and replaces the full body and reference list
(`libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:1004`).

The row lock prevents simultaneous writes inside the transaction. It does not
detect a stale model read. A later stale writer can overwrite a newer body.
Omitting `evidence_refs` erases them.

**Recommended fix:** Require `expected_version`. Reject conflicts with the
current version and return a precise read-merge-retry instruction. Make
`evidence_refs` nullable and preserve them when omitted. Keep revision capture.

### M4. Autonomous scratchpad writes have no server-side spam control

**[medium] `libraries/python/phoebe_admin_agent/prompts/skills/admin_chat_vs_page_workflow.md:34`**

PR #12857 makes note creation part of normal investigation behavior. The
server enforces field sizes, but it has no per-run write budget, duplicate key,
or search-before-write state machine. Scheduled runs and subagents can also use
the four scratchpad mutation tools because of H1 and H2.

Prompt doctrine asks the model to search first. A prompt cannot prevent loops,
retries with new call IDs, or repeated near-duplicate notes.

**Recommended fix:** Add a low per-run mutation budget. Require an idempotency
key derived from scope, normalized title, and durable fact. Reject or merge
near-duplicates within a bounded window. Record a metric for rejected duplicate
and budget-exceeded writes.

### M5. Tool-search eligibility is checked on writes but not at runtime

**[medium] `libraries/python/phoebe_event_agent/runtime_assembly.py:4283`**

PR #12855 defines one external eligible organization and allows all internal
Phoebe organizations
(`libraries/python/organization_feature_settings/__init__.py:59`).
The agent write path rejects an ineligible enable
(`libraries/python/phoebe_admin_agent/admin_org_feature_flags.py:320`).
The normal API write path does the same
(`services/api/routes/admin/orgs/organizations.py:2084`). The web admin surface
uses the API-provided eligibility value
(`apps/web/routes/_app/admin/organizations/feature_flags.ts:109`).

Runtime activation checks only the stored enabled feature and Claude model. It
does not re-run eligibility. A stale, imported, or manually written invalid
feature value can enable tool search for an ineligible organization.

**Recommended fix:** Recheck `is_admin_agent_tool_search_eligible` when the run
bundle is built. Ignore an invalid stored value. Log and count the mismatch.
Add an exact regression test with an ineligible organization whose stored flag
is already true.

### M6. MCP read-only enforcement relies on tool-name words

**[medium] `libraries/python/admin_agent_mcp/tools.py:15`**

Prototype MCP tools are off by default and require an explicit server allowlist.
That is good. However, read-only classification rejects only selected name
prefixes and suffixes. It misses common write verbs such as `edit`, `execute`,
`send`, `upload`, `schedule`, `invite`, `archive`, `move`, and `mutate`.

Every accepted MCP tool is then declared `read_only=True` and
`mutation_scope="none"`
(`libraries/python/phoebe_admin_agent/tools/admin_mcp_tools.py:253`).
The server description or input schema cannot override this claim.

**Recommended fix:** Require explicit per-tool side-effect metadata in trusted
server configuration. Default every unclassified MCP tool to rejected. Use
read-only credentials or a transport-level method allowlist. Treat the name
heuristic as an extra deny rule, not the proof.

### M7. Prompt packs give contradictory mutation and note rules

**[medium] `libraries/python/phoebe_admin_agent/prompts/skills/admin_agent_scratchpad_workflow.md:20`**

The scratchpad pack says to update the canonical note in place. Five lines
later it says to retire and replace any changed fact and “never edit history.”
The later “update in place” and “retire” sections repeat both rules.

The specialized operations pack says every write routes through top-level
approval
(`libraries/python/phoebe_admin_agent/prompts/skills/admin_specialized_operations_workflow.md:21`).
Several tools in that pack, including recorded-turn and Agent Court actions,
are audit-only and do not require approval. The main prompt states the actual
policy correctly
(`libraries/python/phoebe_admin_agent/prompts/internal_admin_agent.md:11`).

**Recommended fix:** Define one note rule: update when the fact remains true;
retire and replace when truth changes. Replace the specialized approval claim
with the actual per-tool policy. Add exact rendered-prompt tests for these
sentences.

### M8. The API still treats an empty vector namespace as an outage

**[medium] `services/api/routes/admin/agent_scratchpad.py:425`**

The library scratchpad search now catches `turbopuffer.NotFoundError` and uses
an empty vector result
(`libraries/python/phoebe_admin_agent/admin_agent_scratchpad.py:723`).
Its exact test confirms no exception log
(`libraries/python/phoebe_admin_agent/admin_agent_scratchpad_test.py:423`).

The API helper still catches every exception together. It logs a stack trace,
returns `None`, and marks search degraded. A new empty namespace therefore
looks like a provider failure in the admin UI and creates noisy error telemetry.

**Recommended fix:** Catch `turbopuffer.NotFoundError` first and return `[]`
without degraded state or an error log. Keep the broad fallback only for real
provider failures. Add the same exact assertion as the library test.

### M9. The note index worker holds a row lock during remote I/O

**[medium] `services/worker/handlers/search_index/admin_agent_notes_process.py:76`**

The worker correctly embeds outside the transaction. It then locks the note row
and holds that lock while awaiting `namespace.write` at line 103. Provider
latency can block note updates for up to the 60-second handler timeout.

**Recommended fix:** Store a versioned provider record without holding a
database lock. After the provider write, use a short compare-and-set update of
`indexed_at`. If the version changed, enqueue the newer version. Another option
is an outbox row keyed by note ID and version.

### M10. A registry mutation can finish before its terminal audit is durable

**[medium] `libraries/python/phoebe_event_agent/admin_tool_controls.py:955`**

The controlled wrapper calls the tool first. It writes the terminal generic
audit afterward. If terminal persistence fails for a successful
production-targeted call, it withholds the result and raises
(`libraries/python/phoebe_event_agent/admin_tool_controls.py:2359`).

The side effect may already exist. The reserved start row remains nonterminal,
and the model receives a failure. Same-call dedup prevents one duplicate path,
but a new tool call can retry an operation whose outcome is unknown.

**Recommended fix:** Require mutation tools to publish their terminal outcome
through an atomic database transaction or durable outbox. For external systems,
persist a call idempotency key and reconciliation state before the request.
Return an explicit `outcome_unknown` error with a readback instruction. Add a
reconciler for stale reserved mutation events.

### M11. Direct write paths default a missing mode to live

**[medium] `libraries/python/phoebe_admin_agent/admin_org_settings.py:266`**

Settings and all four phone writers use
`get_current_mode_if_set() or Mode.LIVE`. Runtime tool context also accepts a
missing current mode
(`libraries/python/phoebe_event_agent/runtime_assembly.py:4524`).

Normal worker assembly should establish mode. If that invariant breaks, these
mutation paths fail open to live instead of refusing execution.

**Recommended fix:** Use `get_current_mode()` for mutation tools. Require
`context.mode` and the current mode to match. Fail with a bounded control error
when either value is missing. Add a no-mode test for every direct writer.

## Low-severity findings

### L1. Scratchpad doctrine cites a stale source line

**[low] `libraries/python/phoebe_admin_agent/prompts/skills/admin_agent_scratchpad_workflow.md:142`**

The example cites `admin_agent_scratchpad.py:683` for vector fallback. The
current implementation starts near line 723.

**Recommended fix:** Cite a stable symbol name, such as
`admin_agent_scratchpad._search_notes`, or update the line reference during
prompt generation.

### L2. Validation-schema failures silently remove retry help

**[low] `libraries/python/phoebe_event_agent/admin_tool_controls.py:1821`**

If `model_json_schema()` raises, `_validation_schema_field_info` catches every
exception and returns no field description or expected value. The model gets a
less actionable validation error, and no log or metric identifies the broken
schema.

**Recommended fix:** Catch known schema-generation errors. Log the definition,
tool, and field once. Count failures. Keep a bounded generic retry hint.

### L3. Admin trace review status hides Temporal errors

**[low] `services/api/routes/admin/agent/agent_traces.py:7399`**

The status route catches all progress-query and completed-result exceptions,
sets progress to `None`, and emits no log or metric. The parser also swallows
all malformed result errors at line 1674. The UI can show an empty progress
state while Temporal or contract parsing is failing.

**Recommended fix:** Catch expected not-ready errors only. Log and count other
query, result, and validation failures with the workflow ID. Return a bounded
degraded reason in the response.

### L4. Malformed artifact handles disappear without telemetry

**[low] `libraries/python/phoebe_event_agent/admin_tool_result_caps.py:1775`**

Artifact parsing tries two schemas, catches all errors, and returns `None`.
Malformed handles can therefore vanish during cap processing without a signal.

**Recommended fix:** Catch `ValidationError`, record the tool name and artifact
shape in bounded telemetry, and keep a warning marker in the visible result.

## Verified controls and non-findings

- The common admin API router applies
  `get_authenticated_admin_user_id` to agent, scratchpad, action-audit, page,
  automation, trace, and telemetry routes
  (`services/api/routes/admin/__init__.py:247`).
- Runtime assembly verifies the internal admin owner or the trusted Slack admin
  actor before it builds the tool registry
  (`libraries/python/phoebe_event_agent/runtime_assembly.py:4193`).
- Admin authentication fails closed to the database when Redis is unavailable
  (`services/api/dependencies/admin.py:39`).
- The web admin agent sends approval decisions to the run-specific endpoint
  with a request ID and current mode
  (`apps/web/components/admin/use_admin_agent_tool_actions.ts:84`). The API
  reloads the trusted run mode, locks the run, and records the admin user with
  the decision (`services/api/routes/admin/agent/agent.py:2523`).
- The scratchpad API sets a request mode, defaulting to live
  (`services/api/routes/admin/agent_scratchpad.py:51`).
- Registry inputs must override `audit_scope()`
  (`libraries/python/phoebe_admin_agent/admin_tools.py:392`).
- Controlled registry calls require an active run and reserve a start audit
  before execution
  (`libraries/python/phoebe_event_agent/admin_tool_controls.py:2141`).
- The standard read-only ORM path uses `SET LOCAL ROLE
  admin_agent_readonly` and verifies `transaction_read_only=on`
  (`libraries/python/phoebe_event_agent/admin_tool_controls.py:167`).
- A static registry test walks read-tool modules and rejects write-capable
  database imports
  (`libraries/python/phoebe_admin_agent/admin_tool_registry_test.py:1540`).
- The source-router client uses transaction-local `app.mode` when a caller
  supplies one, so pooled connections do not retain a prior mode
  (`libraries/python/admin_agent_sources/routing.py:623`).
- The standard result controller applies a universal 16 KiB visible envelope
  and artifact spill before returning large results
  (`libraries/python/phoebe_event_agent/admin_tool_controls.py:734`). The
  750 KiB account-health definition caps do not become 750 KiB inline model
  results.
- No regex-gated workflow pack was unreachable in the inspected selector.
  Exact cold tool names resolve directly, and every suggestion pattern is
  visited in the ordered selector
  (`libraries/python/phoebe_admin_agent/admin_agent_skills.py:2283`).
- PR #12855 guards both supported feature-flag write paths and the web
  eligibility display. M5 is the remaining runtime defense gap.
- PR #12857 fixed the library search scope error order and empty-vector-index
  behavior. M2, M3, M4, and M8 remain.

## Exact-assertion coverage gaps

The project standard asks tests to assert exact IDs, counts, values, and public
strings (`docs/testing.md:96`). The following contracts lack that coverage:

1. **Scheduled surface:** no test unions base and loadable skill tools, then
   asserts that only the exact scheduled mutation allowlist remains.
2. **Subagent surface:** current tests assert a hand-written exclusion set and
   a synthetic human-approval tool. No test asserts that every
   `read_only=False` definition is absent unless explicitly delegated.
3. **DSN rejection:** the guard test covers only `can_insert=True` on one table
   (`libraries/python/admin_agent_sources/routing_test.py:400`). It does not
   cover other table, sequence, schema, function, or inherited writes.
4. **Direct-tool parity:** no contract test requires every mounted admin data
   tool to map to an `AdminToolDefinition`.
5. **Atomic action audit:** no test injects action-audit failure and asserts
   rollback for settings and phone-routing writes.
6. **Mode fail-closed:** no exact test calls each direct writer without mode
   context and expects refusal.
7. **Database query mode:** no exact test requires an explicit mode or proves
   omitted mode cannot mix live and sandbox rows.
8. **Database row bound:** no test proves `limit=None` cannot materialize an
   unbounded result before artifact spill.
9. **Scratchpad scope:** no test asserts the target organization in the generic
   audit event for cross-org note writes or ID operations.
10. **Scratchpad concurrency:** no test uses a stale expected version because
    the contract has no such field. No test proves omitted refs are preserved.
11. **Scratchpad spam:** no test asserts per-run write budget, idempotency, or
    duplicate merge behavior.
12. **API vector 404:** the exact missing-namespace test covers only the
    library tool, not the API helper.
13. **Feature eligibility at runtime:** tests cover supported write paths. No
    bundle test starts with an ineligible organization whose stored flag is
    already true.
14. **MCP classification:** name tests do not prove transport credentials or
    explicit metadata are read-only. Add mutation-shaped tools with neutral
    names.
15. **Terminal audit failure:** no mutation test proves recovery and readback
    after a successful side effect and failed terminal audit write.
16. **Worker lock behavior:** no test proves a slow Turbopuffer write cannot
    block a concurrent note update.
17. **Prompt integrity:** existing tests check selected phrases. No single
    exact policy source is rendered into all packs, so contradictory approval
    and note rules can coexist.

## Remediation order

1. Filter scheduled and subagent registries by capability before skill
   assembly.
2. Make DSN verification exhaustive.
3. Migrate direct tools into the controlled registry.
4. Make state and action audit atomic.
5. Require explicit mode and bounded database reads.
6. Add scratchpad scope, version, and write-budget controls.
7. Recheck feature eligibility in runtime assembly.
8. Replace MCP name inference with explicit trusted metadata.
9. Align prompts and close the observability gaps.
