---
type: reference
tags: [phoebe, admin-agent, audit]
created: 2026-07-14
updated: 2026-07-14
---

# Internal Admin Agent end-to-end audit

**Snapshot audited:** `origin/main` at `af8173bbfb` (read-only detached checkout)  
**Audit date:** 2026-07-14

## Executive summary

Phoebe's Internal Admin Agent is a substantial internal investigation and
operations system, not a thin chat wrapper. A platform admin can launch a
durable INTERNAL agent run from web or Slack; the shared event runtime mounts a
separate typed admin tool registry, records tool-level audits and artifacts,
and exposes live execution, evidence, cost, feedback and review data in a
purpose-built admin UI. It can investigate Postgres/Core/recordings/email/org
state/observability/codebase information, coordinate durable subagents, create
charts and artifacts, run automations, and interact with Slack, Linear, Notion,
GitHub, code sandboxes and Orchard. The corresponding API has 63 endpoints
and the library has 134 registered tool names.

**Maturity assessment:** the read-oriented investigation plane is mature for a
tightly authorized internal audience. Its lifecycle/recovery, typed contracts,
mode isolation, registry controls, audit trail, evidence artifacts, rich trace
review, doctrine packs and test depth are stronger than typical agent
implementations. Its safety story is materially weaker than the slogan
“read-only admin agent” suggests: numerous real external writes are
audit-only rather than human-approved; the Python fallback is not a security
sandbox; the remote sandbox has broad command and cross-run-ownership gaps;
read query/output size and artifact retention have no global hard boundary;
and routine CI does not run live-model investigations. Treat it as
production-usable for inspected, source-backed diagnostics, but **not** as a
safe general autonomy or untrusted-code execution plane without the high-risk
remediations below.

Top takeaways:

1. Internal runs are real shared `PhoebeAgentRun`s with leases, recovery,
   trace/cost persistence, cancellation and streamed events—not an ad-hoc
   admin process.
2. The tool layer is unusually disciplined: a static registry, strict schemas,
   context separation, controlled wrapper, audit reservation/finalization,
   capability manifests, hot/cold packs and artifact spill.
3. The runtime deliberately removes normal iteration/continuation limits for
   Internal/Admin-subagent runs. A 90-minute provider timeout and a $10 alert
   diagnose but do not cap a loop or spend.
4. Database access is typed/catalogued and transaction-readonly with a plan
   cost gate, but the generic query tool has no default row/byte/cell limit;
   low estimated cost does not bound disclosure or context size.
5. Durable callback-first fan-out is real and thoughtful, but its eight-worker
   cap is per execution and there is no global admin-run fan-out/cost ceiling.
6. Slack/web/trace-review/telemetry/automation surfaces are first-class,
   including approval controls and feedback; this is operational software, not
   merely a prompt.
7. Provider integrations intentionally rely on native provider visibility,
   not Phoebe allowlists. Code shows the design, not the deployed OAuth/token
   scope quality.
8. The model cannot merge or push through the GitHub lane. It can, however,
   create reviews/comments and invoke many other external side effects without
   a human approval click.
9. Artifact provenance and output compaction are good, but global artifact
   expiry and universally mandatory output caps are absent.
10. The investigation evals correctly grade evidence, source use and
    uncertainty, but live model runs are opt-in and major write/execution/
    injection paths lack golden e2e coverage.

## System map

The principal flow is: web/Slack ingress creates or resumes an INTERNAL run;
the generic event worker claims its lease and `runtime_assembly` verifies the
admin owner before constructing an admin-only prompt, `AdminAgentToolContext`,
hot tools and loadable doctrine/cold packs. Every controlled tool call then
validates its typed input and policy, reserves an audit row, runs under
mode-scoped context, persists typed artifacts and a shaped output, and
terminalizes the audit row. The model receives compact evidence/ref objects;
the web and Slack surfaces consume events/artifacts while traces, telemetry,
feedback and Agent Court consume the same durable history.

The system deliberately shares foundational machinery with ordinary Phoebe
agents—run/event/turn tables, model selection, provider streaming, tool
approval and worker recovery—while keeping admin-specific registry, source
clients, audit/artifact/subagent tables and prompt packs separate. A normal
General Agent can be launched only through the explicit probe lane; it is not
the Admin Agent. Scheduled automations likewise execute via the normal agent
assembly, so the admin surface authors/observes them but their runtime does
not automatically inherit the internal-admin root contract. External clients
and code-execution lanes sit at the tool boundary, where the weakest safety
assumptions concentrate.

### 1. Runtime core and lifecycle

An Internal Admin Agent session is a normal `PhoebeAgentRun` whose metadata
stamps `internal=true`, `runtime=admin_agent_v1`, `source=admin_agent`, and
`tool_access=read_only` (`libraries/python/phoebe_agent_runs/runs.py:147-165`).
The generic worker claims it under a 30-second renewable lease and runs it
through the shared event loop; it purposely selects an **unbounded** iteration
and continuation-pass configuration for internal and subagent runs, whereas
other agent runs receive 200 iterations / three continuation passes
(`services/worker/handlers/phoebe_event_agent/process_agent_run.py:68-80,594-615`).
That removes an artificial cap but makes lease recovery, provider timeouts,
and cost monitoring the effective circuit breakers.

The bundle builder identifies internal conversations (or Slack admin mode),
resolves and persists the selected tier/model once, rebuilds a pinned prompt,
and returns a `PhoebeAgent` with `AdminAgentToolContext`; its LLM turn timeout
is 90 minutes (`libraries/python/phoebe_event_agent/runtime_assembly.py:5563-5680`).
Tier resolution records selected/resolved tier, reason, model, reasoning effort,
and whether subagents are enabled in both run and conversation metadata
(`runtime_assembly.py:4893-4981`). Internal runtime construction refuses to
continue unless it can verify the INTERNAL conversation owner is a platform
admin (`runtime_assembly.py:3742-4019`; verifier at `3114-3143`).

The generic run loop is event-sourced: it claims the run, drains pending
messages/commands, rebuilds the history and bundle, streams the provider,
persists tool/assistant events, then emits lifecycle events. It detects a
previously RUNNING claim as abandoned and invokes recovery classification before
processing new work (`libraries/python/agent_event_framework/run_loop.py:747-865`).
`RUN_COMPLETED` carries strategy-aware aggregate usage and an execution summary
(wall time, tool call count, passes); pending approval changes the terminal
state to IDLE and notifies the approval surface (`run_loop.py:1530-1605`).

Persistence is dual-purpose: durable conversation runs read `AgentTurn` usage,
while ephemeral projection runs persist equivalent `turn_usage` stamps in
committed event payloads. Both feed a common total of input/output/cache tokens,
context peak, model, and estimated cost (`libraries/python/agent_event_framework/lifecycle.py:401-638`).
At terminal lifecycle events the framework writes run duration and recomputes
conversation token/cost aggregates; crossing $10 in a LIVE conversation sends
one internal Slack alert after commit (`lifecycle.py:703-1005`). Admin trace
metadata and events are thus shared infrastructure rather than an isolated
admin-specific persistence model.

Cancellation is event-based (`InterruptRequestedEvent`), with active commands
terminalized on failed/cancelled lifecycle events; `run_is_active` additionally
returns false once `interrupt_requested` is set (`phoebe_agent_runs/runs.py:135-143`,
`agent_event_framework/lifecycle.py:1007-1048`). Worker leases are heartbeated
every 10 seconds and released in `finally`; a special 25-minute stalled timeout
exists for account-health Slack batch posts (`process_agent_run.py:607-615,839-944`).
Provider errors can retry and switch to a fallback model, with run/recovery/model
metrics emitted by worker hooks (`run_loop.py:1190-1280,1370-1455`).

**Assessment:** lifecycle engineering is mature (idempotent event/command
storage, leases, recovery, cost attribution, prompt pinning). The deliberate
unbounded Internal Admin configuration is a material risk: no admin-specific
iteration, continuation, tool-call, or dollar ceiling is enforced in the run
loop. The 90-minute wall-clock timeout and global high-cost alert diagnose a
bad loop but do not preempt it.

## Capability inventory

### 2. Tool surface, tiering, and capability manifests

`ADMIN_AGENT_TOOL_REGISTRY` is the only static registration point: it contains
134 named tools across 60-ish definition objects, validates unique names,
strict frozen/extra-forbid input and result schemas, declared audit scope, and
an allowlist for every non-read-only mutation scope
(`libraries/python/phoebe_admin_agent/admin_tool_registry.py:167-230`,
`admin_tools.py:198-456`). Every registered tool is wrapped by
`build_controlled_admin_tool_group`, rather than mounted directly
(`admin_tools.py:52-110`). Dynamic MCP definitions may be added at build time;
the runtime eagerly mounts them so that they cannot become unreachable through
cold-pack ownership (`libraries/python/phoebe_event_agent/runtime_assembly.py:3742-3895`).

The static catalog is intentionally much broader than the initial context.
Forty-four measured/high-value names are hot; their schemas mount immediately.
Every other registered tool must have exactly one owning workflow pack; the
prompt renders it as `tool (load pack) — when to use`, and `load_skill` adds
both doctrine and only that pack's cold schemas. Validation rejects an orphan,
an unknown hot tool, two cold owners, and a pack advertising a tool it cannot
mount (`libraries/python/phoebe_admin_agent/admin_tool_tiering.py:1-88`,
`admin_agent_skills.py:1080-1415`). The top-level agent has all tool-backed
skills pre-loaded only for the hot set, plus three settings/phone packs;
admin child runs receive a reduced registry with top-level approval-dependent
write tools removed (`runtime_assembly.py:3799-4019`).

**Complete static tool inventory (134).** Names below are the actual callable
names, not display labels or definition identifiers; registry source is
`admin_tool_registry.py:167-229` unless otherwise cited.

**Investigation routing, records, and data access**

- `inspect_admin_tool_registry_fixture` — synthetic read-only contract fixture.
- `emit_admin_chart_artifact` — persists a declarative `admin.chart@1` chart.
- `report_missing_tool` — audit-only report for an unsupported request.
- `resolve_admin_investigation_context` — resolves ambiguous org/run/URL targets.
- `search_call_recording_index` — finds internal Core meeting recordings.
- `read_call_recording_transcript` — reads a bounded page of a known meeting transcript.
- `search_call_recording_transcript` — searches one meeting transcript.
- `set_call_recording_tags` — replaces the complete tag set on a meeting recording.
- `get_admin_account_health_metrics` — canonical Metrics-page outreach-shift counts.
- `get_admin_account_health_rollups` — bounded multi-organization health rollup.
- `check_admin_core_api_health` — checks controlled Core API availability.
- `resolve_core_account_owner` — resolves an account owner identity.
- `get_core_account_book` — pages the canonical Core account roster.
- `get_core_account_dossier` — fetches a single Core account dossier.
- `get_core_funnel_snapshot` — returns a Core funnel aggregate.
- `list_admin_core_accounts` — lists legacy Core account context records.
- `get_admin_core_account_context` — retrieves one legacy Core account context.
- `list_admin_core_events` — lists Core timeline events.
- `search_admin_core_tam_accounts` — searches Home Care TAM accounts.
- `get_admin_core_tam_account` — retrieves one TAM account.
- `get_org_context_index` — compact targeted organization context.
- `load_org_context_summary` — broader organization summary.
- `list_core_account_emails` — discovers account-scoped Core-ingested emails.
- `get_core_email_message` — fetches one selected redacted email message.
- `get_core_email_thread` — fetches one selected redacted email thread.
- `search_schema` — searches the curated database schema catalog.
- `describe_table` — describes an allowed database table.
- `list_admin_database_queries` — lists fixed curated SQL queries.
- `run_admin_database_query` — executes a chosen curated query.
- `query_admin_database` — executes the constrained domain/query DSL.
- `inspect_admin_organization_membership` — reads an organization roster.
- `invite_admin_organization_user` — creates an organization membership invitation.
- `get_admin_organization_feature_flags` — reads organization feature flags.
- `update_admin_organization_feature_flags` — updates organization flags.
- `survey_org_feature_adoption` — reads feature adoption/surface state.
- `get_org_ehr_state` — reads an organization’s EHR state.

**Runtime evidence, observability, knowledge, and evaluation**

- `inspect_agent_run_trace` — deep one-run timeline, events, tokens, tools, failures, and review hints.
- `compare_agent_runs` — structured two-run trace/cost/tool/termination delta.
- `investigate_token_alert` — correlates a token alert to candidate runs.
- `sweep_agent_health` — broad agent-health assessment.
- `triage_agent_run_observability` — run-level telemetry/observability triage.
- `investigate_posthog_analytics` — bounded PostHog product analytics investigation.
- `investigate_datadog_alert` — Datadog alert investigation.
- `investigate_logfire_records` — Logfire trace/log investigation.
- `investigate_braintrust_evals` — Braintrust eval investigation.
- `search_notes` — searches internal admin-agent notes.
- `search_saved_admin_investigations` — searches saved investigation memory.
- `run_admin_investigation_playbook` — executes a registered investigation playbook.
- `inspect_admin_integration_migration` — reports integration first-party/MCP migration state.
- `map_admin_work_item_to_codebase` — maps work planning input to code evidence.
- `summarize_admin_work_plan` — reads/summarizes an admin work plan.
- `list_agent_court_cases` — lists Agent Court cases.
- `get_agent_court_case` — retrieves one Agent Court case.
- `run_agent_court_judge` — runs a judge analysis for a case.
- `queue_agent_court_justice` — queues Justice work.
- `preview_head_justice_digest` — previews Head Justice digest output.

**Codebase wiki and code data**

- `inspect_codebase_wiki` — routes a broad structure/architecture/symbol wiki request.
- `read_codebase_source_range` — reads a narrow immutable source chunk.
- `search_codebase_symbols` — finds bundle-backed symbol definitions.
- `outline_codebase_file` — returns file imports/headings/symbol outline.
- `search_codebase_source` — searches implementation source text.
- `find_codebase_references` — finds exact identifier references/call sites.
- `search_codebase_knowledge` — searches conventions and architecture docs.
- `inspect_codebase_map` — returns a subsystem/source/test map.
- `inspect_codebase_relationships` — returns imports/importers and unresolved imports.
- `localize_codebase_issue` — localizes a vague code defect.
- `localize_runtime_evidence_to_code` — turns supported runtime evidence into source hypotheses.
- `investigate_codebase` — performs the deterministic DeepWiki investigation bundle.
- `review_codebase_evidence` — LLM reviews the cited deterministic evidence packet.
- `read_run_data` — pages an inline JSON/artifact input.
- `search_run_data` — searches bounded run data/artifacts.
- `slice_json` — projects a JSON path.
- `diff_run_data` — produces a structural/unified input diff.
- `run_admin_python_snippet` — runs a bounded Python transform over supplied run data.
- `list_python_snippet_artifacts` — lists run-data/snippet artifacts.

**External systems and operational writes**

- `search_admin_linear_issues` — searches Linear issues.
- `inspect_admin_linear_issue` — reads a Linear issue.
- `inspect_admin_linear_document` — reads the document attached to a Linear issue.
- `list_admin_github_repositories` — lists accessible GitHub repositories.
- `search_admin_github_pull_requests` — searches pull requests.
- `inspect_admin_github_pull_request` — reads a pull request and review state.
- `get_admin_github_file` — reads a repository file.
- `compare_admin_github_refs` — compares refs/commits.
- `list_admin_github_commits` — lists commits.
- `inspect_admin_github_commit` — reads one commit.
- `list_admin_github_branches` — lists branches.
- `list_admin_github_tags` — lists tags.
- `list_admin_github_workflow_runs` — lists GitHub Actions workflow runs.
- `inspect_admin_github_workflow_run` — reads one workflow run.
- `search_admin_notion` — searches Notion.
- `inspect_admin_notion_page` — reads a Notion page.
- `list_admin_calendars` — lists configured calendars.
- `list_admin_calendar_events` — lists calendar events.
- `inspect_admin_calendar_event` — reads one calendar event.
- `post_admin_github_pr_review` — creates a GitHub PR review; it has no merge endpoint.
- `preview_admin_notion_write` — renders a proposed Notion mutation.
- `apply_admin_notion_write` — applies the explicitly specified Notion mutation.
- `apply_linear_workflow_mutation` — applies a bounded Linear workflow mutation.
- `request_orchard_code_change_task` — sends a structured draft-PR/code-change request to Orchard.

**Subagents, experiments, automation, and outreach**

- `create_sub_agent` — creates one durable delegated child run.
- `create_sub_agents` — creates a bounded batch of delegated child runs.
- `message_sub_agent` — appends a message to an existing child.
- `list_sub_agents` — lists a parent’s children and state.
- `dump_sub_agent` — dumps a child’s task/result details.
- `run_admin_parallel_investigation` — launches the specialized parallel investigation workflow.
- `create_or_resolve_general_agent_probe_org` — creates/locates a seeded sandbox probe org.
- `run_general_agent_probe` — exercises a General Agent configuration under structural guards.
- `manage_admin_agent_automation` — creates/updates/administers admin automations.
- `investigate_outreach_agent_run` — investigates an outreach-linked agent run.
- `inspect_outreach_start_actions` — inspects outreach start actions.
- `reconcile_outreach_suggestions` — reconciles suggested versus real outreach outcomes.

**Sandbox and Slack operations**

- `create_code_sandbox` — provisions a disposable code sandbox.
- `run_sandbox_command` — executes a bounded shell command in it.
- `read_sandbox_file` — reads a sandbox file.
- `write_sandbox_file` — writes a sandbox file.
- `export_sandbox_diff` — exports the sanctioned sandbox work-product diff.
- `destroy_sandbox` — tears down a sandbox.
- `list_sandboxes` — lists and optionally reclaims expired sandboxes.
- `post_account_health_threads_to_slack` — posts account-health threads to the canonical internal channel.
- `post_to_slack_channel` — previews/posts a message to a bot-accessible internal channel.
- `reply_to_slack_thread` — previews/replies to a bot-accessible internal thread.
- `upload_file_to_slack` — previews/uploads a bounded artifact.
- `schedule_slack_message` — previews/schedules a message.
- `update_slack_message` — previews/updates an Admin-Agent-created message.
- `delete_slack_message` — previews/deletes an Admin-Agent-created message.
- `list_slack_channels` — lists bot-accessible channels.
- `get_slack_channel_info` — resolves/validates one channel.
- `search_slack_messages` — searches bounded channel history.
- `get_slack_thread` — reads a bounded thread.
- `lookup_slack_user` — performs an exact Slack user lookup.

The control wrapper validates inputs before calling tools, creates a durable
`admin_tool_audit_events` row at start (including idempotency/in-flight
deduplication), validates the result, persists artifacts, optionally spills a
pre-compaction full result, then caps the inline result and finishes audit
metadata (`libraries/python/phoebe_event_agent/admin_tool_controls.py:167-554`).
Cell and serialized-byte caps first preserve marker/evidence structure,
then remove the largest inline collections and source links while emitting
machine-readable truncation markers. The stated policy is not to silently
truncate, though “no limit” remains the default definition setting
(`admin_tool_result_caps.py:1-210`; `admin_tool_contracts.py:330-385`).

Each admin definition gets an internal-only capability manifest by default:
cross-org data scope, only `INTERNAL_ADMIN_AGENT`, all runtime environments,
LIVE/SANDBOX modes, and audit-only approval unless a stronger side-effect
policy applies (`admin_tools.py:423-469`). The capability page combines those
definitions with general-agent, automation, probe, and dynamic MCP manifests
and reports metadata gaps rather than blocking them (`capability_registry.py:113-209,314-337`).
For a **promotable** capability, manifests must carry shared/admin/general
adapter references, a feature flag, eval/QA evidence and a runtime bound;
`general_live` additionally requires customer-safe output/scope,
observability, and (for promotable) signoff references
(`libraries/python/phoebe_event_agent/capability.py:186-354`). This is a
substantial structural guard, but it validates declaration quality rather than
proving live behavior.

## 3. Data access and evidence contracts

The agent has two deliberately different Postgres access patterns. The generic
`query_admin_database` tool accepts a **typed query DSL**, never a SQL string:
only generated-catalog tables/columns, typed predicates, allowlisted joins,
aggregates, sort and a bounded input shape are compiled into parameterized SQL
(`libraries/python/phoebe_admin_agent/admin_database_query.py:284-535,1088-1342`).
The generated catalog marks columns readable or denied; the resolver applies
that decision to select, filter, aggregate, group, sort and join references
(`admin_database_catalog.py:1-180`; `admin_database_query.py:694-863`). It
therefore does verify the “no free-form SQL” invariant for this public tool.

Before executing, the tool runs `EXPLAIN (FORMAT JSON)` and rejects a plan above
50,000 estimated cost; it then runs inside the read-only admin DB context with
a 60-second local statement timeout (`admin_database_query.py:43-62,2361-2444,
2670-2955`). Query requests declare environment, mode and an organization
scope. Current-org and explicit-org modes add predicates; a cross-org query is
legal only as `all_organizations` and returns a caveat. Catalog queries also
use fixed parameter contracts, strict one-statement `SELECT`/`WITH` validation,
and the same cost gate (`admin_database_queries.py:103-200,1796-1948`).

The native curated catalog covers source-health, org-context shift metrics,
account-health outreach/voice/SMS/shift/contact/callout/clock measures and EHR
historical-volume/cancellation/client-breakdown reports. Its query identity,
parameter allowlist, expected columns and maximum time windows are fixed in
`admin_database_queries.py` (the catalog and binding validation span
`admin_database_queries.py:103-200,1700-1948`). The EHR composite separately
fans out six one-org, live-mode SQL reads—identity, sync health, roster, active
clients/top five, open shifts and configuration—and returns per-source
unavailability rather than turning a failure into zero (`admin_org_ehr_state.py:35-208,541-744`).
Sync error text passes shared secret redaction before it is returned
(`admin_org_ehr_state.py:531-559`).

### Important qualification: output/cost bounds are incomplete

The generic database tool intentionally has **no default SQL row, byte, cell,
or caller limit** (`ADMIN_DATABASE_QUERY_DEFAULT_LIMIT`, `MAX_LIMIT`,
`MAX_RESULT_BYTES`, `MAX_CELL_CHARS`, and `MAX_RESULT_COUNT` are all `None` in
`admin_database_query.py:43-56`). The plan-cost gate limits estimated work, not
result cardinality, response bytes, sensitive-data volume, nor cost accuracy.
The builder only requests one extra row when a caller supplied a limit.
Definitions may choose caps, but global output caps are likewise opt-in. This
is the sharpest data-exfiltration and context-overflow gap in the read-only
query path: an allowed low-cost table scan can still return a very large result.

### Database connection enforcement

`AdminPostgresReadOnlyClient` verifies a connection at pool initialization,
opens each operation in `transaction(readonly=True)`, sets/validates a timeout,
sets `app.mode` transaction-locally, and supports backend cancellation
(`libraries/python/admin_agent_sources/routing.py:240-400`).
`admin_readonly_db_session_context` goes further: `SET LOCAL ROLE
admin_agent_readonly`, `SET TRANSACTION READ ONLY`, checks both state values,
and resets role (`phoebe_event_agent/admin_tool_controls.py:130-159`).

This is strong runtime containment but not a complete privilege proof:
the source-router verifier rejects superusers, `BYPASSRLS`, and INSERT on one
sentinel table, rather than proving absence of all DML/DDL/role-inheritance
privileges. The read-only transaction remains the primary immediate defence.
This should be treated as a **weakened** form of the claimed “readonly role
verified at connect” invariant, not as full authorization attestation.

### Core, org context, flags, and email

Core access is intentionally a small bearer GET client—there is no generic
method or arbitrary URL path (`libraries/python/admin_core_api/client.py:46-144`).
The fixed admin surface provides health, owner resolution, canonical paginated
`core.account_book@1`, single-account `core.account_dossier@1`, aggregate
`core.funnel_snapshot@1`, legacy context/events, and TAM prospect data
(`admin_core_api.py:41-97,365-1058,1829-1966`). Canonical contract payloads
are bounded at 75 KiB/2,000 chars, with full canonical content spilled as a
typed artifact when needed (`admin_core_api.py:67-104,1110-1229,1485-1765`).
Untrusted text in ordinary Core evidence is wrapped before model exposure;
the typed account-book/dossier contracts deliberately keep structured strings
raw, a distinction worth preserving in threat review.

Organization context is a 2 KiB prompt-index plus optional 10 KiB summary.
It concurrently collects app/agent-run state, Core linkage and activity, the
bounded EHR composite, catalog shift metrics, then the dedicated Slack channel
and org-scoped Notion data source; source and total deadlines retain partial
results (`admin_org_context.py:37-69,89-242,438-707,721-933`). The index is
stored in run metadata and injected after the system prompt with a “do not
paste verbatim” guard (`libraries/python/admin_org_context_prompt/__init__.py:7-39`).
Two deliberate semantic exceptions need clear operator understanding: shift
metrics are always LIVE, and product use is declared unavailable and routed to
PostHog rather than being inferred from Postgres.

Core email is a controlled account/message/thread read path: discovery returns
previews, selected message/thread reads redact secrets in text and HTML,
hide HTML from the model, wrap model-visible strings as untrusted evidence,
and attach the complete **redacted** payload as an artifact. Inline body output
is capped at 100 KiB with explicit overflow markers
(`admin_core_email.py:35-55,88-359,432-600`). This is a meaningful protection
against prompt injection and accidental copying, but the full redacted HTML
artifact still preserves customer-sensitive content for anyone with artifact
access; it is not a least-privilege substitute.

Feature flag/settings tools use a separate controlled update lane and are
covered in the write/approval assessment below. The underlying admin flag cache
is Redis-backed and its updater publishes a namespaced cache invalidation
(`libraries/python/admin_flags/initialize.py:13-43`,
`libraries/python/admin_flags_sync/reconcile.py:25-40`); it is not the same
thing as per-organization feature flags.

### Snowflake: intentionally removed, but documentation is stale

There is no Admin Agent Snowflake source client, query catalog, tool, or
credential setting in this commit; `AdminSourceRouter` rejects `snowflake`
(`libraries/python/admin_agent_sources/routing_test.py:1122`). The current
curated-query plan explicitly says the agent “no longer exposes Snowflake tools
or needs Snowflake credentials” and records their removal
(`docs/notes/plans/pho_13589_admin_database_curated_queries_execplan.md:14-32,232-324`).
Older plans—including the still-marked-in-progress PHO-13417 document and
PHO-13556 context design—describe Snowflake query/catalog functionality that
does not exist in this source tree. This is a documentation and operator-risk
arc, rather than an unimplemented safety mechanism.

## 4. Subagents, fan-out, and General Agent probes

Admin subagents are not ephemeral function calls. `create_sub_agent` and the
batch form make `admin_investigation_executions`/`admin_investigation_tasks`
records under a verified internal parent, then create a hidden
`PhoebeAgentRunType.SUBAGENT` child, enqueue an init event, and subscribe its
terminal events back to the parent (`subagent_tools.py:270-511,893-1137,2905-3090`;
`subagent_child_runs.py:296-417`). Tasks carry the parent org/mode/run identity,
a server-generated contract, target routing metadata, per-child token/cost/tool
counts, and a structured terminal payload. The task contract explicitly says
model-proposed source/tool names are hints rather than authorization and bars
production/config/permission/external-user actions (`subagent_quality.py:103-289`).

Fan-out is queued and callback-first. The active cap is eight **per generic
admin execution**, with additional tasks persisted as queued; creation locks
the execution row, launches in ordinal order, and terminal finalization starts
the next queued child (`subagent_payloads.py:45-80`; `subagent_child_runs.py:916-1030,1456-1640`).
The response asks the parent to wait for `child_completed`, avoid timer polling,
and avoid premature first-child synthesis (`subagent_tools.py:861-889`).
Completion callback state is deduplicated/validated against child run, terminal
event and final text before it is treated as the primary fan-in signal
(`subagent_completion_callbacks.py:55-265`). Follow-up has deterministic
target/relevance checks, at most 20 stored messages and normally five terminal
follow-ups, and reuses/relaunches the durable task rather than appending an
unscoped chat (`subagent_payloads.py:45-80`; `subagent_tools.py:1469-1790`).

The cap is **not global**: it applies to each execution, which is keyed by
parent run. There is no demonstrated organization-, tenant-, queue-, or
system-wide admission ceiling. An internal user can thus create an unbounded
number of parent runs/executions, each with eight live children and an
unbounded durable queue. This is both a cost/availability risk and a direct
counterexample to a broad reading of “bounded pool.”

The worker contract has thoughtful result hygiene but only lightweight quality
calibration. A structured terminal result includes outcome, reason, evidence,
confidence, blockers and a next action; an “answered” result with any evidence
is assigned `needs_review`, explicitly advisory and not an authorization gate
(`subagent_quality.py:291-481`). Weak outcomes can auto-relaunch up to twice,
stepping Haiku→Sonnet→default, but only while an execution slot is free;
otherwise the skipped escalation is recorded (`subagent_child_runs.py:1570-1640,
2410-2605`). There is no independent semantic judge or empirical calibration
in this mechanism. General-agent children intentionally have no weak-result
escalation policy (`subagent_child_runs.py:1642-1780`).

There are two parallel-investigation mechanisms, often easy to conflate.
`run_admin_parallel_investigation` is a registered **ledger compiler**: it
accepts target/result records and produces a typed artifact; missing results
remain explicitly unresolved (`admin_parallel_investigation.py:1-528,619-847`).
The separate runtime helper persists an execution/task ledger and uses an
in-process semaphore to invoke supplied worker coroutines concurrently
(`admin_parallel_investigation_runtime.py:123-580`). It does not create child
chats. Its `max_worker_count` accepts 1–16, targets max 64, and its timeout is
documentation-only; neither layer establishes a global budget or recovery
worker. The report inventory’s “parallel investigation” tool should therefore
be read as ledgering, not a general model-delegation launcher.

### General Agent probe

The probe deliberately instantiates the normal General Agent assembly against
an explicit organization, while persisting an admin task and a hidden wrapper
child run for traceability (`admin_general_agent_probe.py:180-286`,
`admin_general_agent_probe_orchestration.py:74-330`). Live probes are forced
read-only. Sandbox writes require the faithful profile, `read_only=false`, a
seeded admin probe org, a feature gate, approval-gated tool classification and
a maximum of 50 writes (default 15); standard read-only probes reject requested
write tools (`admin_general_agent_probe_contracts.py:113-405`; `admin_general_agent_probe.py:1458-1510`).
The runtime policy independently counts unique calls and blocks unknown,
non-allowlisted, excessive and write-cap calls (`admin_general_agent_probe.py:377-430`).

This is a serious structural probe surface, but the code’s own defaults leave
fast and faithful max turns/tool calls as `None` (`admin_general_agent_probe_contracts.py:32-49`), and the probe process-local concurrency semaphore is 8.
Thus test runs have no hard loop bound except the underlying agent/runtime and
no cross-process admission control. “wait_for_subagents” observes/drains
children and reports fan-in; it deliberately leaves parent synthesis to normal
durable callbacks rather than attempting synthesis in the probe
(`admin_general_agent_probe_fan_in.py:83-190`).

## 5. External integrations and write boundaries

### Primary runtime integrations

Linear, GitHub, Notion and Google Calendar have native typed clients—not
generic MCP—as the production read path. The external API tool group only
issues fixed reads and wraps all provider-returned strings as untrusted
evidence before model exposure (`admin_external_api_tools.py:60-97,102-947,
1129-1200`). It exposes Linear issue/document reads; GitHub visible-repo, PR,
file/diff, ref, commit, branch, tag and workflow-run reads; Notion search/page
reads; and Calendar list/read operations. Provider identity is intentionally
the resource boundary: installed GitHub repo visibility, Notion sharing and
Calendar ACLs determine access, not a duplicate Phoebe allowlist
(`admin_external_api_tools.py:1202-1289`).

The inventory calls Linear/GitHub/Notion/Calendar/Slack/Granola
`api_native_primary`, while generic MCP is `prototype_or_smoke_only` and
requires explicit prototype enablement at runtime
(`admin_integration_migration_inventory.py:1-585`). MCP configuration still
has auto-discovered/read-allowlisted Linear, GitHub, Calendar and Granola
server defaults when its environment credentials exist
(`libraries/python/admin_agent_mcp/config.py:1-500`). That is an intentional
fallback/prototype capability, but makes configuration drift and accidental
surface reactivation worth monitoring. Granola’s production route is Core
ingestion plus the call-recording index/transcript tools; MCP is comparative
only. No durable production write path exists for Calendar or Granola.

Datadog, Logfire, Braintrust, and PostHog are investigation tools through the
source router. They expose bounded input *shapes* rather than arbitrary
provider endpoints: Datadog monitor/event/log/span investigation; Logfire
five fixed query intents; Braintrust reference/summary/comparison/failing
example intents; and a generated HogQL template for PostHog aggregate,
raw-event, session, and flag contexts
(`admin_datadog_investigation.py:198-823`,
`admin_logfire_investigation.py:80-654`,
`admin_braintrust_investigation.py:56-450`,
`admin_posthog_tools.py:1-722`). Logfire and Braintrust wrap external text;
Datadog’s summaries are structured and capped by contracts. All are marked
read-only.

### The exception: PostHog is structurally constrained but effectively unbounded

PostHog builds its query from typed parameters, so it does not accept raw
HogQL (`admin_posthog_tools.py:258-527`). Yet its `limit` is optional, and
definition-wide maximum result count, bytes, and cell chars are all `None`
(`admin_posthog_tools.py:22-25,657-787`). The raw/session templates select the
entire `properties` object, and returned rows are passed through without
`wrap_untrusted_evidence` or redaction. Therefore an unbounded, untrusted,
potentially PII-bearing event payload reaches model context. The prompt tells
the model to regard results as untrusted, but that is not the enforced wrapper
used elsewhere. This is a high-priority safety inconsistency.

### External writes: audit-only, not human-approval gated

The registry has three native external write lanes:

| Lane | Boundaries actually enforced | Approval posture |
| --- | --- | --- |
| Linear `apply_linear_workflow_mutation` | Live-only; one issue/project/comment/relation action; typed input; before snapshot and best-effort after snapshot | `admin_linear_workflow_approval_tool_names()` is empty; manifest says `AUDIT_ONLY` |
| Notion `apply_admin_notion_write` | Live-only; exact UUID; only create page/data-source record or append page blocks; size/source-link limits; deterministic preview token | Preview reports `approval_required=False`; approval list is empty |
| GitHub `post_admin_github_pr_review` | Live-only; admin run required; configurable repository allowlist; fixed review endpoint; 20 findings; stale-head converts inline to summary; dedupe fingerprint | approval list is empty; manifest says `AUDIT_ONLY` |

Evidence: `admin_linear_workflows.py:41-416,760-840`,
`admin_notion_workflows.py:60-475,720-810`, and
`admin_github_pr_review.py:54-570,543-580,699-855`.

These write lanes do produce normal admin-tool audit rows through the common
wrapper and their own result/audit metadata. But “audit-only” is a materially
weaker posture than the stated distinct human-approval write lane: a model can
apply real Linear/Notion/GitHub writes once it has the tool. In particular,
the Notion preview token is a deterministic hash of the requested content, not
a persisted, user-approved authorization; the apply path recomputes it and
does not reread/compare `expected_current_summary` with Notion. It prevents
argument mismatch, not stale-target writes or unauthorized intent.

The GitHub lane is correctly narrow: it has **no merge, push, PR creation,
branch, workflow edit, issue edit, or repository-settings operation**. It can
only post a PR review to an allowlisted repo, so the “no merge authority”
invariant is verified despite the stale migration-inventory entry that describes
the older native GitHub surface as read-only.

## 6. Slack surface: gated internal command plane, not merely a notification sink

Slack has two materially different paths. The normal V2 ingress turns an
app-mention or a bound-thread reply into a debounced durable run event; the
Admin Agent path first asks for the explicit admin-mode prefix and then
upgrades the same run/conversation to `INTERNAL`. `build_slack_admin_mode_plan`
requires V2, a configured internal workspace, an explicit request, and the
resolved actor capability (`slack_ingress/admin_mode.py:41-76`). Runtime
assembly independently re-verifies the latest event actor, user admin flag,
internal email, and every thread workspace before it will build tools
(`phoebe_event_agent/runtime_assembly.py:3237-3345`). This is a genuine
fail-closed second gate, not just ingress metadata.

The policy deliberately permits more than one eligible internal admin to
continue an existing admin thread: every new message refreshes
`current_actor`; a person may only *start* admin mode on a pre-existing
non-admin thread if they own the run (`admin_mode.py:79-214`). That is the
right operational collaboration behavior, but it means ownership is not an
ongoing conversation ACL. An operationally posted thread is marked as a
platform-admin-originated admin run and therefore also accepts eligible
internal-admin replies without the `admin:` prefix
(`slack_ingress/admin_operational_threads.py:79-280`).

### Ingress, context, and controls

* Webhook signature verification happens before queueing, the target Slack
  team must map to an organization, and a paused organization is dropped
  (`services/api/routes/webhooks/slack.py:27-128`). `process_slack_event_ingress`
  repeats the org/team match and resolves the Slack user through the linked
  account mapping (`slack_ingress/__init__.py:1197-1318`). An unmapped mention
  writes a mapping stub and receives a signed, expiring account-link URL by
  DM rather than starting a run (`user_link.py:25-60`,
  `slack_ingress/__init__.py:696-706,1454-1499`).
* Only the app mention starts an unbound discussion. Plain messages must be in
  a bound thread and are ignored if addressed to a different human; an actor
  also must be a mapped coordinator to start/routinely continue a run
  (`slack_ingress/__init__.py:708-770`). Quiet channels drop events; read-only
  and maintenance channels reject recognized mutating outreach intents before
  enqueue (`681-799`). Lifecycle controls require `can_manage_channel`.
* `!aside` is a strict local non-routing tag: the inbound message and any
  fetched context entry with that tag are omitted (`__init__.py:671-679`,
  `side_effects.py:137-218`). The system also handles mute/unmute/sleep/archive
  keywords and avoids queuing non-keyword messages to a silenced thread.
  This makes the tag a useful anti-noise convention, not a confidentiality
  boundary—Slack still retains the message and the model could receive it via
  another tool.
* For a mention within an existing human thread, and for replies to an admin
  thread, the bot fetches up to 12 surrounding Slack messages. It excludes the
  current message and `!aside`, preserves bot/human attribution, and is
  best-effort on Slack failure (`side_effects.py:137-218`,
  `__init__.py:1177-1338`). Admin-mode translations preserve that context;
  the runtime prompt calls it untrusted evidence and tells the model to ignore
  role/tool-policy instructions (`runtime_assembly.py:1499-1520`).
* A V2 inbound event that actually enqueues work receives a best-effort
  `:eyes:` reaction after commit, with latency metrics. It is an acknowledgement
  of queueing, not a trace diagnosis or completion signal
  (`side_effects.py:299-395,425-450`). Trace diagnosis itself is an operational
  report route: a judge-triage report can create/bind an internal agent thread
  with its source metadata.
* A V2 app mention inside an existing *unbound* thread goes through a durable
  handoff state machine: it reserves a row before Slack I/O, spawns/anchors a
  Phoebe thread, and retries/reconciles stale reservations
  (`services/api/routes/webhooks/slack_thread_handoff.py:67-250`; ingress
  orchestration `__init__.py:1339-1446`). This provides a web-thread bridge
  rather than silently attaching the agent to arbitrary human discussion.

### Slack writes and delivery

The `AdminAgentToolContext` adds an internal Slack response user ID and is
only passed to Slack-specific groups for an already verified Slack-admin run
(`runtime_assembly.py:3888-3914`). The runtime prompt requires exactly one
final `post_to_slack_thread` reply, in Markdown, for that originating thread
(`1499-1519`). In parallel, generic admin Slack tools can post/reply/upload,
schedule, update, delete, search, list channels, inspect a thread, or lookup
a user. All real writes require live mode and bot-accessible C/G channels;
the bot token is checked with `auth.test` against configured internal team IDs
and excludes DMs, non-member, and archived channels
(`admin_slack_client.py:30-159,270-363`; `admin_slack_workflows.py:116-250`).

Those controls are scope controls, not human approval. Eight registry entries
are `read_only=False`, including post/reply/upload/schedule/update/delete and
`post_account_health_threads_to_slack`; their mutation scope is
`slack_workspace` (`admin_slack_registry.py:101-188` and following). They use
dry-run defaults and common audit logging, but an agent can set
`dry_run=false` in live mode without a user approval card. This is another
concrete exception to “read-only tools plus a distinct approval lane.”

Artifacts have two Slack delivery modes. A tool may upload a bounded generated
file to an allowed internal channel, while the normal run result is rendered
as a thread response. Operational reports post their root outside the run
loop, immediately bind it to an internal run, post detailed replies
best-effort, then eagerly add the “Continue in this thread” anchor. Bind
failure triggers a compensating Slack delete; a block-render failure retries
text-only once (`admin_operational_threads.py:152-334`). This is a thoughtful
delivery design, although post/reply failures are intentionally not retried
because the write is not idempotent.

Account-health posting is one such operational delivery lane. It groups the
complete account array into one internal Slack thread per organization, has an
explicit instruction to retry the exact same call on partial failure, and is
registered as a write (`admin_slack_registry.py:139-158`; implementation in
`phoebe_slack/slack_automation_tools.py:1164-2140`). Its downstream batch flag
and scheduler are assessed in Domain 8.

## 7. Web UI: mature inspection surface over a pinned internal chat

The primary `/admin/agent` screen is a single internal-admin-org chat surface,
not an organization picker. It creates internal runs in the pinned default
admin organization; cross-org investigation is expected to happen through
tools rather than a client-controlled run scope
(`apps/web/routes/_app/admin/agent/admin_agent_content.tsx:425-520`). It has
run history (search/status/archive), title editing, archive/restore, a saved
investigation drawer, tier selector, image attachment, transcript copying,
and run/link/trace navigation. New messages are optimistically shown and
deduplicated by `client_operation_id`; the frontend then follows the admin
variant of the event stream and refreshes run metadata/artifacts as the cursor
advances (`components/admin/use_admin_agent_chat_session.ts:28-205`,
`use_admin_agent_chat_stream.ts:25-139`, `use_admin_agent_run_actions.ts:93-535`).

The chat reuses `PhoebeAgentEventFeedChatSurface` but forces `userIsAdmin`,
top-level tool execution cards, admin artifact renderers, and a minimal tool
presentation (`use_admin_agent_chat_stream.ts:84-102`,
`admin_agent_chat_panel.tsx:48-114`). A pending-approval card can send a
single/batched allow-or-deny decision, idempotenced with a UUID; a stop button
posts an interrupt request (`use_admin_agent_tool_actions.ts:42-145`). The UX
exists and is correctly stream-driven. Its practical safety value is limited,
though: the major Slack/Linear/Notion/GitHub write definitions currently use
`AUDIT_ONLY`, so they do not normally produce an approval card.

### Execution, artifacts, and answer quality

The run screen supports a chat view, execution graph, raw payload-file view,
and responsive right-side/drawer inspectors. The graph joins persisted trace
nodes and current stream tool entries, then exposes reasoning, tool inputs,
output previews, child runs, related source/child runs, metrics, and iMessage
debug state (`admin_agent_content.tsx:560-850`,
`agent/inspection/execution_inspector_panel.tsx:1-1600`). The run-event
inspector shows up to 12 current committed/pending rows and lets the operator
open/download structured JSON (`run_event_inspector.tsx:1-418`).

Artifact UX is unusually complete: artifacts may render inline from tool or
assistant refs (including compatibility hoisting of adjacent legacy refs), in
the run sidebar, and as download-ready JSON. The registry covers chart,
visualization, full tool result, evidence bundle, codebase wiki atlas,
investigation ledger, token breakdown, outreach reconciliation, and three
trace-timeline keys (`artifacts/list.tsx:1-219`, `artifacts/registry.ts:1-65`,
`admin_run_artifacts_panel.tsx:25-198`). The chart renderer validates a
declarative Zod payload before rendering and caps displayed points at 5,000
(`artifacts/chart.tsx:1-130`). This is a good UI-side containment layer, but
it does not replace backend result bounds.

Completed answers have structured human feedback: category/note plus linked
artifact and ledger evidence are posted against the exact agent turn/item;
the run page also lets an operator save a durable investigation snapshot
(`admin_agent_content.tsx:760-960`; controls in
`admin_answer_feedback_controls.tsx`). The saved-investigations UI displays
target ledger, findings, artifact references and links back to the source run
(`saved_investigations.tsx:1-990`).

### Trace review and telemetry

`/admin/agent-traces` is a broad operational review UI, not just Admin Agent
runs. It supports full-text query, created/cost/duration/first-response/judge
flag sorting, pagination, bulk human-owner assignment, and rich filters for
organization, owner, source, conversation type, backend, prompt versions,
review/LLM-judge state, labels, feedback, urgency, and auto-draft status
(`agent-traces/index.tsx:1-690`, `trace_filter_bar.ts:1-420`,
`trace_table.tsx:1-540`). Detail view has transcript/debug event timelines,
model/run metrics, execution graph, related subagent navigation, contextual
Admin Agent chat, LLM judge runner, Agent Court “justice” action, rerun,
synthetic-query generation, and trace message injection
(`agent-traces/$conversationId.tsx:624-1288,1145-1613`).

Human review supports owner, status, disposition, notes, mutable annotations,
and links to Linear; judge annotations are visible but not removable by the
human annotation control (`$conversationId.tsx:3524-4065`). The index review
dialog can refresh deterministic auto-tags over a date range and optionally
run the LLM judge over unjudged traces; it also toggles the global
`AGENT_TRACE_LLM_JUDGE_AUTO_RUN_ENABLED` flag from the UI
(`review_dialog.tsx:155-420`). This is a significant production control
surface: route authorization and audit—not client-side state—must carry the
safety burden (audited in Domain 15).

The landing page gives concise links to capability registry, missing-tool
reports, automations and telemetry. Capabilities renders catalog owner,
release stage, audiences/runtimes/modes/environments, side-effect class,
approval requirements, data scope, customer-safe-output indicators, flags,
eval/QA metadata and metadata gaps; it is a read-only inventory view
(`capabilities.tsx:83-500`). Missing Tools aggregates model-reported gaps by
requested slug with report history/artifact IDs and live/sandbox filtering
(`missing-tools.tsx:34-450`). Tool telemetry presents calls, failures/denials,
latency and error categories; source-health chips show Postgres, Datadog,
PostHog, Logfire, Braintrust and Core API status across environments
(`tool-telemetry.tsx:33-939`, `source_health.tsx:1-300`).

### Agent Lab

Agent Lab is a launch UI for repeatable General Agent probes, not a separate
sandbox. It writes a normal Admin Agent run whose initial prompt encodes a
template, target, capability/flag manifest, mode and probe profile. Available
templates are recommendation subagent fanout, capability promotion smoke, and
sandbox write/approval smoke (`agent_lab_config.ts:8-111`,
`agent_lab_launcher.tsx:56-310`). The client blocks a sandbox-write choice
unless it targets a seeded probe organization and the browser mode is sandbox;
it forces a faithful profile for that option (`agent_lab_launcher.tsx:480-590`).
Those are useful guardrails, but they are entirely advisory until the
server-side prompt parser/probe contracts enforce them (Domain 4 confirms the
latter partially does).

## 8. Automations, account health, operations, planning and Agent Court

### Automation control plane and scheduler

`admin_agent_automations` is a small control plane over the general
`AgentAutomation` runtime rather than a second scheduler. Its single tool
supports list, inspect, preview-create, create, update, pause, resume,
archive, and run-now. Creation/preview accepts at most one schedule or event
trigger family (up to 20 event triggers) and **requires** a finite
`valid_until` or `max_runs`; update revalidates that the resulting safety
object remains bounded (`admin_agent_automations.py:102-191,279-332,1423-28`).
It stamps `source=internal_admin_agent`, uses the internal run/template
contracts, and has a quiet-output default. `run_now` locks the automation,
requires active state except for a dry run, forbids a concurrent active run,
and auto-pauses it when a bounded control is exhausted
(`admin_agent_automations.py:955-981,1015-1150`).

This is one of the exceptions to “read-only”: the catalog definition is
`read_only=False, mutation_scope="agent_automations"`
(`admin_agent_automations.py:1481-1510`). That scope maps to
`PHOEBE_PRODUCTION_MUTATION`, which is an approval-required policy
(`phoebe_event_agent/admin_tool_contracts.py:103-134`), so authoring or
changing an Admin-owned automation uses the explicit approval lane rather
than merely audit-only external-write treatment.

The underlying scheduled worker sweeps every minute, leases due triggers with
`FOR UPDATE SKIP LOCKED`, and runs under global concurrency 20; event
dispatch sweeps independently every five seconds and also has a global 20
cap (`services/worker/handlers/phoebe_event_agent/automations_sweep.py:1-175`,
`automation_dispatches_sweep.py:1-179`). A schedule is version-pinned and
records the next fire only after a claimed execution; expiry/max-runs produce
a skipped run and pause the automation. Failures back off and eventually
pause. Event dispatches are separately leased, expire on a TTL, re-check the
entity/current status/version at launch, cap active event runs per
automation, and dead-letter after a retry limit
(`agent_automations/scheduled_triggers.py:67-350`,
`agent_automations/event_dispatches.py:77-121,141-299,511-570`).

Idempotency is good but is **at-least-once around external effects**. An
event launch first looks for an `AgentAutomationRun` with the same
automation/event and resumes/marks the dispatch rather than duplicating it
(`event_dispatches.py:235-256`); each new run has a stable queue external ref
`automation:<run-id>:<reason>` (`executions.py:358-470`). The durable
automation record, leases, active-run check, and event identity prevent the
ordinary double-launch. They cannot make a tool’s downstream side effect
exactly once if the worker dies after the effect but before its tool/run state
commits; individual write tools must supply their own idempotency.

Automations ultimately run a normal `ConversationType.GENERAL` agent
(`agent_automations/executions.py:410-435`). Thus their actual side-effect
posture derives from the general runtime’s capability/tool approval policy,
not merely the automation authoring validator. The runtime has an explicit
automation approval-tool list, but it is not a categorical ban on all
autonomous actions; this distinction matters for any capability intentionally
made automation-safe.

### Daily account-health pulse

The account-health job is a live-only hourly tick that claims the 07:00 ET
daily window in process. It requires both
`ACCOUNT_HEALTH_AUTO_RUN_ENABLED` and the separately default-false
`ACCOUNT_HEALTH_BATCH_ROLLUP_ENABLED`; otherwise it exits and logs the
batch-rollup skip (`account_health_run.py:60-115`,
`admin_flags/cache.py:289-310`). A Redis-style daily state machine uses a
36-hour lock with claiming/run/done states; it recovers the same day’s
internal run, locks the run before prompt dispatch, and persists a
`prompt_dispatched` marker (`account_health_run.py:307-406`). This gives
strong duplicate suppression, although a failure between dispatch and marker
commit still relies on run/event deduplication downstream.

It creates a live **internal** conversation under a fixed internal
organization and an asserted admin owner. Context loading is best effort—on
failure the pulse proceeds without it (`account_health_run.py:328-357`). The
canonical prompt is unusually prescriptive: retrieve the eligible-org catalog
exactly once; issue fixed, server-resolved batches of 25 (never >50); use only
the batch rollup; treat a failure as atomic and retry a smaller batch; never
fan out per-org SQL/PostHog; and post the complete ordered list exactly once
to Slack (`services/worker/handlers/system/account_health_prompt.py:1-20,
113-122`). This materially constrains a model loop, but remains prompt-level
policy rather than a full workflow state machine.

`get_admin_account_health_rollups` is a fixed production read-only aggregate
lane. It limits a cohort to 50 organizations, slices one organization/local
day, and runs dependent query families under an eight-minute atomic deadline:
any failure returns no partial account rows (`admin_account_health_rollup.py:100-257,517-525,701-778`). The final Slack operation receives up to 250
accounts in one ordered call. Its durable idempotency key derives from run ID
and the organization set; a retry skips operation threads already posted,
which is appropriate protection for the model’s specified partial-failure
retry (`phoebe_slack/slack_automation_tools.py:2090-2118,2808-2863`).

### Operational investigation workflows

The health sweep is a deterministic, read-only, bounded fanout across
Datadog, Logfire, run health and source health; PostHog/RUM and voice are
explicitly skipped. It compacts the response and spills capped evidence and
visualizations as artifacts (`admin_agent_health_sweep_workflow.py:1-400`,
`admin_agent_health_sweep_compaction.py:1-300`). Observability triage accepts
at most five runs, optionally consumes that sweep and a Braintrust regression
query, correlates evidence, and produces a **bug-report preview**, not a
ticket (`admin_agent_observability_triage_workflow.py:117-139,293-438`).

Outreach investigation requires an exact UUID/outreach URL and reconstructs
the bounded linked-run/timeline evidence, optionally exposing raw events;
the reconciliation workflow independently compares suggested/start/attempt
and decision-event coverage using Postgres plus Logfire. Both declare
`read_only=True` (`admin_outreach_investigation_workflow.py:1-260`,
`admin_outreach_reconciliation_workflow.py:1-450`). Work planning is likewise
a bounded, read-only join of Linear, GitHub, and optional Notion documents;
it returns source coverage, confidence, caveats and links rather than making
planning mutations (`admin_work_planning.py:60-213`).

### Agent Court

Agent Court read tools list/detail cases through the admin readonly database
session. The three analysis actions are writes: `run_agent_court_judge`,
`queue_agent_court_justice`, and `preview_head_justice_digest` are
`read_only=False` under `agent_court_analysis`
(`admin_agent_court_workflows.py:823-958,2330-2372`). That scope is classified
as `EXTERNAL_WORKFLOW_OUTPUT`, so it is audited but not in the two
approval-required classes (`admin_tool_contracts.py:103-134`). The Justice
queue locks the case, requires a nonfailed judge result, reuses/creates with
`idempotency_key=manual_agent_trace:<conversation-id>`, and queues work; it
does not itself apply a recommendation. Head Justice is a preview whose
artifact can include an LLM analysis but does not post to Slack
(`admin_agent_court_workflows.py:1300-1340,1840-1848`). The distinction is
important: these tools mutate review/audit workflow state even though they do
not directly change a customer record.

## 10. Knowledge, playbooks, memory and context scaffolding

### DeepWiki/codebase wiki

The Codebase Wiki is a substantial local/hosted source-index subsystem, not a
thin grep wrapper. It indexes a pinned git revision into files, chunked source
with exact line/source URLs, parsed symbols/outlines/import edges, Markdown and
steering-document knowledge chunks, and a deterministic subsystem/route map;
the atlas/bundle cache is keyed by revision and can recover a missing bundle
(`admin_codebase_wiki.py:796-1084,1086-1170,16027-16180`). Its advertised
surface has 13 read tools:

- `inspect_codebase_wiki`, `read_codebase_source_range`, `search_codebase_source`, `search_codebase_symbols`, `find_codebase_references`, `outline_codebase_file`, and `inspect_codebase_relationships` for grounded navigation.
- `search_codebase_knowledge` for docs/conventions/steering separate from implementation chunks; `inspect_codebase_map` for topology.
- `localize_codebase_issue`, `localize_runtime_evidence_to_code`, and `investigate_codebase` for the structured routing/investigation path.
- `review_codebase_evidence`, a single LLM review pass constrained to cited deterministic evidence. `inspect_codebase_wiki` remains the older multi-intent entry point.

`investigate_codebase` explicitly composes knowledge/source/symbol/import
search, narrow reads, tests, runtime handoff, candidate/ruled-out evidence,
confidence and next reads (`admin_codebase_wiki.py:3165-3281`). Evidence review
drops uncited LLM findings (`3284-3397`). Its plan forces map → knowledge →
source chunks → outline/symbol/import relationships → narrow source read before
calling a candidate source-backed (`admin_codebase_wiki.py:9181-9200`). Visible
results are compacted and full output spills to an artifact (10 KB / 6K-cell
limit) (`16422-16531`).

The system is deliberately honest about missing layers: no language-server
references, dynamic call-graph traces, branch/diff awareness, ownership
history, or persistent vector index (`admin_codebase_wiki.py:16501-16518`).
The atlas/bundle cache itself writes local/S3 cache artifacts despite the tool
being declared `read_only`; that is a non-customer metadata side effect and
should be documented as such rather than treated as a literal no-write
guarantee.

`map_admin_work_item_to_codebase` is the higher-level adapter: it turns an
external Linear/GitHub/Notion/meeting/manual item into DeepWiki candidates,
related tests, source label/freshness/caveats, subagent fanout plan, and a
**Linear-comment preview**. Applying that preview requires the separate
external-write tool (`admin_knowledge_workflows.py:54-709,720-776`).

### Playbooks

There are six production catalog playbooks—not an open-ended generated
catalog: high-token Datadog alert, failed agent run, repeated tool-call loop,
organization health summary, outreach missing-contact reconciliation, and
staging/prod regression (`admin_investigation_playbook_library_catalog.py:46-1009`).
The public library can list/select/run; selection is deterministic token
overlap with ties requiring an explicit ID, inputs are declared/coerced before
source work, and completed runs receive a playbook-level evidence-bundle
artifact (`admin_investigation_playbook_library.py:55-260,300-430`).

The generic runner is a credible small workflow engine: it validates required
inputs/bindings, records step progress, supports declared branches/stop
conditions, invokes only the supplied controlled tool group, gathers evidence
and expected artifacts, and derives a validated terminal state
(`admin_investigation_playbook_runner.py:53-370`). The authoring helpers force
each catalog item to state input types, source/resolver requirements, tool
steps, artifacts, success criteria and next-action rules
(`admin_investigation_playbook_authoring.py:21-172`). This makes evidence
expectations testable. The limitations are coverage and selection: six
workflows leave most admin domains to prompt/skill discipline; keyword
selection is explainable but shallow.

### Durable historical memory

`search_saved_admin_investigations` reads an organization- and mode-scoped
snapshot table, first narrows at SQL then scores up to 100 candidates and
returns at most 10. It carries trace/alert/error/model/tool/category filters,
relevance reasons, a 30-day stale indicator, partial-coverage caveat, source
run path, and wraps visible historical text as untrusted evidence
(`admin_saved_investigation_search.py:45-191,274-428,431-635`). This is good
operator memory, but deliberately not an auto-answer: results are frozen,
ranked contextual suggestions that must be rechecked.

`search_notes` is a distinct hybrid-search memory over saved agent notes,
with optional caregiver/client/outreach/run/time/tag filters, 50-result cap,
500-character snippets and 300-character extracted content
(`admin_agent_notes_search.py:41-253,307-344`). It is more general but does
not imply that notes are authoritative incident history. The UI’s saved
investigation action (Domain 7) is the write path for durable snapshots; no
model-autonomous “remember this conclusion” tool was found.

### Doctrine, hot/cold schemas, and organization context

Twelve workflow/doctrine packs cover trace, token cost, codebase, subagents,
account health, source health, General Agent probe, Slack operations, GitHub
repo operations, Python snippets, code sandbox, and specialized operations
(`admin_agent_skills.py:301-1050`). Each owns cold tool schemas; build-time
validation requires every cold tool to have exactly one owner and prevents hot
tools from being re-declared cold (`admin_agent_skills.py:280-286,1251-1382,
1570-1585`). Runtime appends an accurate catalog and requires `load_skill` to
mount a pack’s methodology and schemas together; request pattern matching emits
a preserved hint for only the relevant packs (`runtime_assembly.py:2214-2280,
2885-2929`; `admin_agent_skills.py:1704-1764`). This is a thoughtful context
budget design, but classifier hints are advisory: the model may still choose
the wrong pack or never load one.

On run creation, organization context is gathered concurrently from app/Core,
EHR and catalog data, followed by Slack/Notion when app identity succeeds.
Each source has a 2.5-second budget under an 8-second total deadline; failures
become explicit unavailable fields. The index includes identity/kanban/last
touch/recent Core email and calls/Slack/product runs/open callouts/shifts/EHR/
Notion and product-usage availability, then trims itself to 2 KB in a stable
order (`admin_org_context.py:52-76,122-145,614-709,728-856`). It is stored in
run metadata and injected once into the system prompt with “do not paste
verbatim” guidance (`admin_org_context_prompt/__init__.py:7-31`,
`runtime_assembly.py:3962-3965`). It is useful scaffolding but can be stale by
later turns, and account-health proceeds even if its best-effort auto-load
fails (Domain 8).

## 11. Prompts and surface-specific doctrine

I read every Markdown prompt under `libraries/python/phoebe_admin_agent/prompts/` (the two root prompts and 24 skill prompts). The design is deliberately a compact, cacheable root plus on-demand doctrine—not one giant always-mounted instruction set.

### Root assembly and cache behavior

- `_build_internal_admin_agent_system_prompt` starts with the normal Phoebe base prompt, but for the new base truncates it at `## Tool use policy`; that removes the customer-facing tool catalog before adding `internal_admin_agent.md` (`libraries/python/phoebe_event_agent/runtime_assembly.py:1448-1476`). The comment accurately explains the purpose: the admin runtime must not be nudged toward unavailable scheduling tools.
- `_load_internal_admin_prompt` substitutes the concrete untrusted-evidence markers into the checked-in root/skill text (`runtime_assembly.py:472-484`). The 107-line root prompt scopes the agent to internal admin, makes unavailable capability reporting mandatory, distinguishes quick lookup / diagnostic / approval / automation modes, mandates scoped read-only subagents, and tells it to treat every returned source—including database cells, Slack context, trace payloads, and child results—as evidence rather than instructions (`prompts/internal_admin_agent.md:3-107`).
- A generated prompt then receives only: an optional Pro section, the list of available workflow skills, a registry-derived cold-tool catalog, an optional Slack response section, and a frozen organization-context index (`runtime_assembly.py:3915-3965`). This is cache-friendly: the selected system prompt is a provider ephemeral-cache prefix and the index is frozen in run metadata for byte stability across turns (`runtime_assembly.py:3986-3991`). Prompt component versions and SHA-256s are stamped from the exact resolved agent after build, avoiding a cache-refresh build/stamp skew (`runtime_assembly.py:5752-5801`).
- Resumed pinned prompts get a compatibility repair only for a missing cold-tool catalog, rather than a wholesale rewritten prompt (`runtime_assembly.py:2937-2958`). This preserves historical prompt behavior but means an old stored root itself remains old; the safety benefit of a new root instruction does not retroactively apply to pinned runs.

### Root contract and doctrine packs

The root does contain a strong no-soft-limit doctrine: if context, time, or soft cost pressure interrupts an investigation it must checkpoint, preserve hypotheses/evidence, and name the next read-only step—not invent a conclusion (`prompts/internal_admin_agent.md:100-107`). It directs the model to use artifacts/source evidence rather than narrate tool mechanics and to reserve `load_org_context_summary` for broad recaps. It does **not** literally promise “never deny/truncate”: instead the output-size behavior is implemented by result caps/artifact spill and further governed by each skill.

The Pro addition is a narrowly targeted parallelism instruction: resolve the full target ledger, use batch fan-out for truly independent bounded targets, carry target IDs/contracts, wait for `child_completed` callbacks rather than routine polling, and never delegate mutable/risky/unclear work (`prompts/internal_admin_agent_pro_tier.md:1-58`; appended only when the tier enables subagents at `runtime_assembly.py:3933-3936`). The 12 general operational packs are selectively mountable: account health, code sandbox, codebase investigation, General Agent probe, GitHub operations, organization settings, phone routing, run-data/Python, settings location, Slack operations, source health, specialized operations, subagents, token cost, and traces. (The registry distinguishes workflow skills from the eval-generation packs; the first group has 15 prompt files.) The runtime instructs the agent to load a relevant pack before deep work and mounts its methodology plus schemas together; cold tools are unknown-to-call until that happens (`runtime_assembly.py:2885-2929`).

Key prompt-level controls are concrete rather than generic:

- Account-health doctrine defines live-mode defaults, known metric gaps, Core-account identity contracts, exact outreach status semantics, and `admin.chart@1` output (`prompts/skills/admin_account_health_metrics.md:1-90`).
- Codebase/trace/token-cost doctrine tells the model to read a narrow range before source-backed claims, keep source and runtime evidence distinct, start from exact target IDs, and inspect compact attribution before broader logs (`admin_codebase_investigation.md:1-56`, `admin_trace_investigation.md:1-68`, `admin_token_cost_investigation.md:1-54`).
- Slack doctrine keeps writes in dry-run until exact content/destination are confirmed; sandbox doctrine prohibits PHI, DSNs, and credentials and designates exported diff artifacts as the only sanctioned work-product exit (`admin_slack_operations_workflow.md:1-53`, `admin_code_sandbox_workflow.md:1-63`). These are behavioral guidance, not independent enforcement.
- Organization-settings and phone-routing doctrine explicitly says each write is approval-gated, reads first, sends only changed fields, and never claims success without `updated`/`created` (`admin_organization_settings.md:1-183`, `admin_phone_routing.md:1-119`). This is aligned with the enforced production-mutation lane for settings but wider than the actual audit-only external/admin metadata write lanes discussed below.
- The run-data/Python skill describes native data operations first and a constrained Python fallback, with same-run inputs/outputs and audit requirements (`admin_python_snippet_workflow.md:1-67`).

### Surface variants

| Surface | Prompt/runtime variation | Assessment |
| --- | --- | --- |
| Web Internal Admin Agent | Base-minus-customer-catalog + root contract + hot tools + loadable doctrine/cold catalog + frozen org index. | The primary, deliberately compact surface. It does not receive the regular customer-tool catalog. |
| Slack Internal Admin Agent | The same admin prompt gets a final-response clause requiring exactly one `post_to_slack_thread`, Markdown output, and treating prior thread context as untrusted investigation seed (`runtime_assembly.py:1499-1520,3950-3952`). | A real surface-specific policy and tool-context binding, not merely cosmetic formatting. |
| Admin subagent | Appends a small explicit child-runtime block: complete the delegated task, give a concise parent answer, and do not treat persisted task fields as authorization/scope (`runtime_assembly.py:3922-3931`). | Good structural hardening against task-payload escalation; it shares the parent admin tool family subject to wrapper policy. |
| General Agent probe | This is not a different Internal Admin root. The `admin_general_agent_probe_workflow` is admin doctrine for launching/assessing a normal General Agent, selecting fast vs faithful profile and sandbox/live policy (`prompts/skills/admin_general_agent_probe_workflow.md:1-56`). | Important distinction: it prevents report readers from mistaking a probe’s General Agent system prompt for an admin prompt. |
| Automations | Scheduled automations run through the regular agent assembly, with agency/playbook/automation skill additions rather than `_build_internal_admin_agent` (`runtime_assembly.py:4600-4640`). | Automation authoring is accessible from admin, but execution does not inherit the admin root prompt by construction. |
| Eval-generation agents | Nine dedicated Case Sentinel/Cartographer/Writer/Repair prompts are schema-constrained stages. They emphasize seeded fixtures, strict JSON, independently checkable evidence, no agent-visible grading leaks, and bounded replay repair (`admin_eval_generation_skills.py:45-183`; `prompts/skills/case_sentinel.md:1-71`, `demyst_case_writer.md:1-185`, `multi_step_case_writer.md:1-98`). | This is unusually detailed doctrine for a narrow workflow, but it is separate from ordinary investigation prompting. |

The nine eval prompts form a guarded pipeline: Sentinel decides create/skip based on durable behavior; Cartographer chooses single- vs multi-step; independent world-facts, caregiver-memory, and harness-shape checkers must approve/reject; writers use overlays and structured graders; repairers must preserve the original signal rather than delete difficult fixture facts (`case_cartographer*.md`, `demyst_replay_repair.md:1-85`, `multi_step_replay_repair.md:1-48`). This substantially reduces eval “answer leakage” and invented fixture risk. The main residual is enforcement: many requirements are LLM prompt rules, with validation models/tests providing only partial structural backstop.

## 12. Artifacts and bounded output

### Durable artifact contract and spill path

An `AdminToolArtifact` is a typed JSON envelope: `artifact_type`, positive `artifact_version`, title/summary/lifecycle, source and evidence metadata, optional time window/caveats, and an object payload. Its model-visible counterpart, `AdminToolArtifactRef`, contains only identity/type/version/title/summary/lifecycle (`libraries/python/phoebe_event_agent/admin_tool_contracts.py:222-310`). This is a meaningful boundary: the wrapper persists full artifacts **before** applying result-size caps, so model transcripts and event feeds receive only compact durable references (`admin_artifacts.py:412-545`). Persistence failure withholds the result rather than returning a dangling reference.

The generic overflow behavior is well designed:

1. The controls add an `admin.tool_result.full@1` artifact containing the pre-compaction result whenever a tool opts into spill or a capped result needs one (`admin_tool_controls.py:594-640`). Existing nested artifact payloads are stripped in that full-result copy to avoid recursive payload growth.
2. `apply_admin_tool_result_caps` caps cells, then trims largest inline collections, then source links; it reports explicit caveats and `truncation_markers` with a reference to full data where available (`admin_tool_result_caps.py:68-144,412-517`). If the result still cannot fit after envelope and marker compaction, it fails rather than silently producing an oversized transcript (`admin_tool_result_caps.py:974-1030`).
3. A preview replaces full artifacts with a fixed-length placeholder ref before cap decisions, preventing a cap pass/fail difference caused merely by a random UUID (`admin_artifacts.py:583-612`; `admin_tool_result_caps.py:381-400`).

This supports the intended “digest + artifact” behavior mechanically, but only on definitions that set size caps or request spill; as noted in the safety section, caps are not globally mandatory.

### Storage, scopes, and retention

Artifacts are rows in `admin_artifacts`, not an object-store abstraction: the DB stores JSONB payload/source/evidence/caveats, binding each row to organization, mode, run, and optional audit event. The schema has strong mode RLS and deletes artifacts when the parent run is deleted (`database/schema.sql:4388-4429`). `_insert_artifact` stamps the live context organization/mode/run and audit ID rather than trusting a tool-provided location (`admin_artifacts.py:733-765`).

The web API lists and reads only rows matching the already-verified internal run plus its organization and mode; an artifact ID from another run consequently returns 404 (`services/api/routes/admin/agent/agent.py:2103-2216`). The UI fetches summaries first, lazily fetches one full artifact, selects a renderer by `type@version`, and can locally download the payload as JSON (`apps/web/routes/_app/admin/agent/admin_run_artifacts_panel.tsx:27-197`). There is no server-side generic CSV/download route: JSON is the web export format.

I found no general artifact TTL or purge worker. Uploaded images alone get an `expires_at` field in their JSON payload (30 days in production, 7 elsewhere), with file type/5 MiB/25 MP validation and metadata stripping, but no read-query enforcement or deletion path in the inspected artifact module (`admin_artifacts.py:40-65,131-189,218-276,576-580`). Thus ordinary artifacts—including full tool results, trace evidence, email data, and sandbox diffs—appear to inherit run-retention/deletion rather than an explicit artifact-retention policy. This is a material data-retention gap, especially because payloads are database JSON rather than expiring object URLs.

### Artifact families and renderers

The storage contract is generic JSON, not a fixed “table/CSV/text artifact” taxonomy. The concrete families I found include full tool results, investigation ledgers, codebase-wiki atlas/bundles, evidence bundles, token breakdowns, trace timelines, outreach reconciliation, Core account/email artifacts, call-transcript artifacts, Python/run-data outputs, sandbox command/diff output, external API payloads, Orchard tasks, Agent Court data, missing-tool reports, and user-uploaded images. The web renderer registry currently has specialized components for registry fixtures, charts, codebase wiki, evidence bundles, ledgers, token breakdowns, full results, visualization, outreach reconciliation, and three trace-timeline aliases; unknown types safely receive a fallback renderer (`apps/web/routes/_app/admin/agent/artifacts/registry.ts:18-62`).

There are two structured plotting families:

- `admin.chart@1` accepts only line/bar/area with typed axes and named numeric series. It caps to 32 series and 5,000 total points, validates axis values, persists source/evidence/time-window metadata, and intentionally records a spec rather than source rows (`admin_chart_artifacts.py:25-162`; `admin_chart_artifact_tool.py:62-145`). The frontend Zod-validates it before rendering and refuses malformed charts (`artifacts/chart.tsx:132-171`).
- `admin.visualization@1` is a richer inspectable schema for lines/bars/stacked bars/scatter/histogram/waterfall/graph/metric cards/timelines/dashboards; it carries row/series/metrics/nodes plus provenance and raw-data links, explicitly preferring structured facts over screenshots/Markdown tables (`admin_visualization_artifacts.py:20-245`). The web renderer only supports a subset of those kinds at present (line/bar/stacked_bar/waterfall/metric_card/timeline/dashboard), so valid scatter/histogram/graph output has a renderer capability mismatch (`artifacts/visualization.tsx:32-42`).

“Tables” are JSON payload/renderers such as full-result, evidence, and ledger artifacts, not a first-class table schema; “text” is likewise payload data rather than a dedicated text artifact. CSV/PDF/image are supported as **Slack upload MIME types**, not native durable artifact types. That separation is important for callers expecting a downloadable CSV artifact.

### Slack versus web delivery

The web exposes durable artifacts by run-scoped JSON and separately serves only uploaded-image bytes with private five-minute caching; it intentionally removes image base64 from the normal JSON response (`agent.py:729-753,2144-2182`). Slack’s `upload_file_to_slack` accepts explicit generated text/base64 bytes (exactly one), bounded to 1 MB and a small MIME allowlist—JSON, PDF, PNG/JPEG, CSV, Markdown, or text—and uses Slack’s external-upload protocol only on `dry_run=false` in live mode (`admin_slack_contracts.py:33-50,325-388,687-700`; `admin_slack_workflows.py:203-330`). It is audit logged and channel-gated, but it does **not** take an `AdminToolArtifactRef` or copy an existing durable artifact automatically. The model must reconstruct/provide file bytes; delivery is therefore a separate write lane with potential content duplication, not a controlled export of the stored artifact.

## 13. Code execution and code-change lanes

This area has three materially different capabilities. Treating all as “sandboxed code execution” would be misleading.

### Run-data Python: constrained process, not a security sandbox

`run_admin_python_snippet` is intended as an escape hatch over explicit already-fetched JSON, after native `read_run_data`, `search_run_data`, `slice_json`, and `diff_run_data`. Inputs are limited to inline JSON or artifacts from the *same* admin run and mode; named steps resolve only within that run and can be source-hash pinned on rerun (`libraries/python/phoebe_admin_agent/admin_python_snippet.py:239-325,1461-1685`). Native verbs avoid user code entirely and have narrow page/search/diff limits.

The subprocess guardrails are substantial for accidental resource abuse: 20 KB code, 5 MiB total inputs, isolated/site-disabled Python (`-I -S`), cleared environment, closed stdin, wiped temporary CWD, 5–10-second wall timeout, 5 CPU seconds, 32 MiB file limit, 64 file descriptors, and 512 MiB address-space limit on Linux (`admin_python_snippet.py:90-171,762-873,2479-2633,2645-2655`). Stdout/stderr are previewed to 8 KB and named/truncated output is preserved as a durable artifact with code/input/output hashes plus audit metadata (`admin_python_snippet.py:818-873,2671-2744,2985-3035`).

Its security boundary is nevertheless weak: this is an in-container process as the worker user, with no VM, network namespace, seccomp, chroot, or filesystem allowlist. The module itself records the caveat that “v0 subprocess isolation is not VM or kernel-grade network isolation” (`admin_python_snippet.py:2739-2742,3327-3359`). `-I -S` and an empty environment do not stop stdlib `socket` networking or absolute-path reads available to the worker account. The prompt says not to use network/API access, but the runtime does not enforce it. This is the most important execution safety gap in the system.

### Remote code sandbox: genuine remote VM, but broad command authority

The seven sandbox verbs create/clone, run command, read/write `/workspace` files, export a git diff, destroy, and list/reclaim (`admin_code_sandbox.py:55-67,236-673`). They route to an exe.dev backend using a short-lived (default 120 s), operation-scoped signed token; the coordinator token lets a given operation invoke only high-level `cp/new/tag/rm/ls/ssh` commands and embeds the run ID/operation (`admin_code_sandbox_client.py:23-51,257-291,567-627`). Sandboxes receive the `phoebe-admin-agent`, run, and creation tags and are rechecked after creation; operations refuse a sandbox that lacks the global admin tag (`admin_code_sandbox_client.py:308-385,520-565`; `admin_code_sandbox.py:589-617`). Command/file output is digested, wrapped as untrusted evidence, and spilled to artifacts; every operation includes an audit digest and run/user/mode metadata (`admin_code_sandbox.py:771-907`).

The safety posture is mixed:

- File helper reads/writes resolve paths under `/workspace`; write content and environment values reject a small hard-coded collection of credential/PHI/DSN substrings, modes are at most `0755`, and the default presentation is only 6,000 characters (`admin_code_sandbox.py:104-165,746-768`; `admin_code_sandbox_client.py:792-854,901-929`).
- The main `run_sandbox_command`, however, accepts arbitrary shell text and an arbitrary `workdir`, then executes `shell=True` using `os.environ.copy()` inside the VM (`admin_code_sandbox.py:297-330`; `admin_code_sandbox_client.py:387-410,741-789`). It can trivially bypass the helper’s `/workspace` path policy (for example by running `cat` itself). The static sensitive-text blocklist is not a secrets or exfiltration control, and no egress restriction is established in Phoebe code. The manifest’s claim of network-injected read-only repository credentials is an assumption about the VM/provider, not a verified enforcement in this checkout (`admin_code_sandbox.py:964-990`).
- Run ownership is not checked. `_tagged_sandbox_operation_run_id` finds any globally tagged admin sandbox, returns its embedded run ID, and allows operations; `list_sandboxes(run_id=None)` can enumerate all tagged sandboxes. A known ID from another admin run is therefore potentially operable across runs/users (`admin_code_sandbox.py:589-617,620-673`).
- TTL cleanup is opt-in through a mutating `list_sandboxes(reclaim_expired=true)` call (default 180 minutes), not an automatic sweeper. “Only diff artifact may leave” is doctrine and output design, not a technical prevention of command-output exfiltration (`admin_code_sandbox.py:472-537,676-743`).

### Orchard draft-PR handoff

`request_orchard_code_change_task` is a live-only, audit-only admin mutation that submits one bounded task to the configured Agent Session Coordinator / Orchard plane. Inputs are capped; repo is fixed to `phoebe-health/phoebe`; source paths are repository-relative; and the caller must supply a patch plan or evidence reference (`admin_orchard_code_change.py:48-245,311-433`). The dispatch payload sets `autopr=true`, `draft_pr_requested=true`, and `merge_allowed=false`, with a prompt requiring latest `origin/main`, a draft PR only, no auto-merge, bounded runtime, and tests (`admin_orchard_code_change.py:436-477,572-606`). It stores an artifact recording dispatch status and evidence, with request prose wrapped as untrusted evidence (`admin_orchard_code_change.py:656-743`).

No human approval is required beyond the model’s normal ability to invoke this audit-only mutation. More importantly, draft/no-merge is sent as metadata and text, not locally enforced at execution: the code explicitly says the separate Orchard auto-PR infrastructure “must enforce draft behavior at execution time” (`admin_orchard_code_change.py:719-724,747-809`). A new UUID session ID is also used for every call, so a retry after ambiguous network/session failure is not intrinsically idempotent; the recovery guidance merely tells the caller to inspect the coordinator before retrying (`admin_orchard_code_change.py:480-552`). This lane is properly separate from the API worker but depends on an external enforcement boundary that this audit cannot verify.

### Eval generation is staged LLM/replay work, not direct source editing

The eval-generation executor injects one preloaded prompt-only stage skill, adds a single `submit_eval_generation_stage_output` tool, limits each LLM stage to eight iterations, and Pydantic-validates the submitted JSON; it records usage per stage (`admin_eval_generation_runner.py:28-119,132-230`). The `agent_court` pipeline then runs Sentinel -> Cartographer -> three independent checks -> writer -> replay/repair, recording every input/output. Missing or rejecting checkers block approval; replays have a 900-second wall timeout and repairs are capped at eight (`agent_court/eval_generation/runner.py:161-231,234-405,667-787`; `schemas.py:8-18,537-677`). Fixture feasibility is generated from the seeded inventory, explicitly preserving required shifts/caregivers and the failure signal (`runner.py:893-1051`).

This is strong provenance and schema discipline for generated eval definitions. It does not itself mutate source, create PRs, or give the stage agent broad tools. The outstanding risk is operational: the replay executors are protocols implemented elsewhere, so the actual seeded-org/multi-step execution isolation and production-data separation are not proven by this library alone.

## 14. Evals and test coverage

### Investigation-eval architecture

The dedicated evaluation suite is a real-admin-runtime test with deterministic
evidence, not a canned-answer test. There are **37** `AdminInvestigationEvalCase`
fixtures in `evals/suites/admin_investigation/golden_investigations.py`; the
runner creates a test org and admin user, creates an `INTERNAL` agent run with
the same metadata that the API uses, dispatches the question through the normal
event path, rebuilds the normal runtime bundle, and replaces every mounted
admin tool group with fixture-backed mocks (`evals/runners/admin_investigation_runner.py:397-444,634-692`). A repeated fixture result is consumed in call order;
an unconfigured tool receives a generic success payload. Redis/event-publishing
side channels are temporarily no-op'ed under a global patch lock, so this is a
single-process runtime exercise rather than an external-services integration
test (`admin_investigation_runner.py:346-395`).

The model reasoning is live, but cases are skipped unless `ANTHROPIC_API_KEY`
is present; plain CI executes the deterministic rubric tests only
(`evals/admin_investigation_eval_test.py:49-78`; operating instructions in
`docs/development_guides/admin_agent_evals.md:15-33`). That is a sound
fixture-isolation design, but it means a green default CI run does **not**
demonstrate real model behavior. The eval runner also fixes
`max_iterations=12`, unlike production Internal runs' unbounded loop
configuration (`admin_investigation_runner.py:634-654`), so it cannot expose
long-running/cost-loop behavior.

Each case declares required/forbidden tools, argument regexes, min/max counts,
maximum total calls, first tool, ordered subsequences, fixture-fact regexes,
source/transparency, lifecycle state, hypothesis/evidence ledger, ambiguity,
overconfidence, next step, and unsupported-source-claim rules
(`evals/schema.py:561-649`; evaluator at
`admin_investigation_runner.py:78-289`). Built-in attribution guards reject
claims that Datadog, PostHog, database, or trace data was seen without the
corresponding tool call. The runner records call/output streams and explicitly
tests callback fan-in: before the last synthetic child-completion batch, a
visible parent answer or `dump_sub_agent`/`list_sub_agents` polling is a
failure (`admin_investigation_runner.py:507-563,703-764`). This is an unusually
good evaluation philosophy for investigations: it grades source use and
epistemic behavior as well as final facts.

### What the golden cases cover

The 37 scenarios cover token alerts and high-token/clamped traces; trace
lookups/comparison; cross-org and cost-gated database queries; Core account
book/dossier/funnel/churn analysis; result/artifact paging and JSON/run-data
inspection; call-recording search/quotation; PostHog; an account-health chart;
source-health and monitor state; three outreach-resolution/reconciliation
traps; organization settings; fan-out/callback waits/status; missing-tool
reporting; and feature-adoption ambiguity. The complete case declarations are
visible at `golden_investigations.py:88-4414`; their design guide requires
distinctive fixture facts, real-user phrasing, source citations, appropriate
hedging, and a concrete follow-up (`docs/development_guides/admin_agent_evals.md:35-92`).

There is broad unit coverage beneath that suite: **68**
`libraries/python/phoebe_admin_agent/*_test.py` files (and 1,360 named test
functions across the library) cover registry/controls, query compiler and
curated queries, Core/email/org state, trace/compaction/token tools, all
investigation providers, artifacts, prompts/tiers, subagents, General Agent
probe, Slack workflows, automations/account-health/Court, external API
workflows, codebase wiki, Python, sandbox, Orchard, and eval generation. The
route package adds seven agent-route test modules, including API auth/stream,
automations, Court, traces, iMessage, and prompt-rollout tests. This is strong
module-level coverage, especially of schemas, typed validation, policy
selection, and failure paths.

### Material coverage gaps

The golden suite is nevertheless not a full capability matrix. It has no
golden cases for Slack thread ingress/reflection or real Slack writes,
automation authoring/scheduling/account-health batch rollups, Agent Court
justice actions, Linear/Notion/GitHub mutations, code sandbox ownership/egress,
Orchard dispatch enforcement, Python sandbox escape/egress, saved
investigations/feedback, or approval UX. It also has no direct adversarial
prompt-injection evaluation proving every evidence-producing tool wraps
untrusted text, no systematic all-tool read-only/import-guard regression case,
and no live-provider test of OAuth/token scopes or the actual readonly DB role.
The fixture stubs mean neither RLS nor provider pagination/rate limits nor
artifact storage/retention is exercised end-to-end.

Several of these have unit tests, but that distinction matters: a unit test of
the wrapper cannot establish that an external audit-only write is acceptable
under an attacker-controlled Slack/thread payload. Add a required live-model
CI lane (or recorded approved-model replay) for the golden cases, then add
negative/e2e cases specifically for injection, approval interception,
cross-run sandbox ownership, output-size abuse, and every non-readonly tool
family.

## 15. API surface and persistence model

### Route mounting and authorization chain

All paths below have the `/admin` application prefix. `services/api/app.py`
mounts the admin router at that prefix; `services/api/routes/admin/__init__.py`
places every listed agent subrouter inside `api_router`, which has the inherited
`get_authenticated_admin_user_id` dependency (`app.py:157-170`,
`routes/admin/__init__.py:163-196,272-279`). That dependency authenticates the
user, consults a short-lived Redis allow/deny cache, falls back to a database
`User.admin` read on cache miss/error, and returns 403 by default
(`services/api/dependencies/admin.py:31-83`). Thus a handler without an
individual `Depends` is still admin-gated. Several global dashboards use
`get_no_rls_db_session`; that is a deliberate cross-org/cross-mode read choice,
not an unauthenticated route. It increases the importance of the inherited
router dependency and of review on any future alternate mounting.

`agent/__init__.py` and `agent_saved_investigation_snapshot_helpers.py` define
no endpoints. The following is the complete 63-endpoint surface in the route
files at this revision; suffixes are relative to `/admin`.

| Module / path | Methods and purpose |
| --- | --- |
| `agent.py` `/agent/runs` | `POST` creates an INTERNAL run (optionally page context and initial message); `GET` lists an org/mode's internal runs; `GET /{run_id}` returns durable turns, usage, and audit summary; `GET /{run_id}/investigation-tasks` returns execution/task graph. |
| `agent.py` run control | `PATCH /{run_id}/model`, `PATCH /{run_id}/tier`, and `PATCH /{run_id}` pin model, select tier, or rename/archive; `POST /{run_id}/interrupt` enqueues interruption; `POST /{run_id}/tool-decisions` records approve/reject of pending gated calls and wakes the worker. |
| `agent.py` discovery and data | `GET /agent/source-health`, `/agent/capabilities`, and `/agent/missing-tools` expose configured-source probes, the staged capability registry, and grouped missing-tool artifacts. `POST /agent/runs/{run_id}/images` creates a validated image artifact; `GET /artifacts`, `GET /artifacts/{artifact_id}`, and `GET /artifacts/{artifact_id}/image` return run-scoped artifacts/image bytes. |
| `agent.py` interaction/events | `POST /agent/runs/{run_id}/messages` idempotently queues a user message (or routes a subagent follow-up and mirrors it to a bound Slack thread). `GET /events` returns a cursorable snapshot/feed. `GET /events/stream` is newline-delimited JSON long-poll streaming, with independent event/pending-insert/pending-status cursors, capped limits, run-mode-scoped sessions, and investigation-task state enabled (`agent.py:2219-2481`). |
| `agent_answer_feedback.py` | `GET /agent/answer-feedback` lists an org/mode's feedback; `GET /agent/runs/{run_id}/answer-feedback` lists one run; `POST` at the same run path creates categorized feedback tied to a validated assistant item/turn plus optional artifact/ledger refs (`agent_answer_feedback.py:193-314`). |
| `agent_saved_investigations.py` | `POST /agent/runs/{run_id}/saved-investigations` creates a denormalized, frozen answer/evidence/task/usage snapshot; `GET /agent/saved-investigations` lists by org/mode/state; `GET /agent/saved-investigations/{id}` loads it. The source run is first discovered cross-mode, then all contents are read under its mode (`agent_saved_investigations.py:816-949`). |
| `agent_tool_telemetry.py` | `GET /agent/tool-telemetry` aggregates a 1–90 day all-org/mode window: call/failure/output volume, drill-down rates, trends, largest outputs and recent failures (`agent_tool_telemetry.py:746-809`). |
| `agent_automations.py` | `GET /agent/automations` lists automation groups/targets; `GET /agent/automations/runs` lists cross-org execution/run state with pending-approval filters; `GET /agent/automations/attention` lists dead-letter dispatches and failing schedules that may never have made a run (`agent_automations.py:140-365,479-700`). |
| `agent_run_metrics.py` | `GET /agent-traces/duration-trends` returns bucketed duration/first-generation percentiles and in-flight/long-running counts, optionally by run type, mode and org (`agent_run_metrics.py:227-275`). It is mounted before the trace catch-all. |
| `agent_traces.py` list/review | `GET /agent-traces` is the large filtered/paginated top-level trace explorer; `GET /annotation-groups` and `/annotation-traces` expose label aggregates/drill-down. `PUT /{conversation_id}/review`, `PUT /{conversation_id}/owner`, and `POST /owner-assignments` persist human disposition/Linear link and ownership. `POST /{id}/review-notes`, `POST /{id}/review-annotations`, and `DELETE /{id}/review-annotations/{annotation_id}` manage human review material; judge annotations cannot be deleted (`agent_traces.py:5058-5880,5883-6209,6442-6481`). |
| `agent_traces.py` judge/playground | `POST /{id}/judge` runs and persists an LLM judge; `POST /{id}/justice` requires that judge, creates/queues an Agent Court case; `POST /review` starts a time-window Temporal review workflow and `GET /review/{workflow_id}/status` exposes it. `GET /{id}` returns full trace, prompt/version, events, relationships, cost, review and iMessage debug. `PATCH /{id}/model`, `POST /{id}/rerun`, `/messages`, `/interrupt`, and `/tool-decisions` operate the Admin Playground—the decisions endpoint intentionally returns 409 because playground runs do not support pending approvals. `POST /generate-synthetic-query` makes a small Haiku call for a test prompt (`agent_traces.py:6277-6584,6587-7474`). |
| `agent_court.py` | `GET /agent-court`, `/by-conversation/{id}`, and `/{case_id}` list/resolve/read cases. `GET /recommendations/{id}/playbook-review` gives a proposed playbook change; `POST .../approve` applies or creates a playbook item only after admin review; `POST .../reject` marks it rejected. Non-playbook targets are explicitly redirected to GitHub review (`agent_court.py:388-688,774-1013`). |
| iMessage modules | `GET /imessage/staff/{user_id}/diagnostics` audits one staff recipient's notification gates/budget. `GET /imessage/threads`, `/threads/{id}`, and `/filter-options` provide all-org LIVE staff-iMessage thread inspection (`imessage_staff.py:117-190`; `imessage_threads.py:245-440`). |
| `prompt_rollout.py` | `GET /prompt-rollout` reads version/cohort/auto-training rollout state; `POST /{prompt_key}/promote` atomically promotes the active candidate, clears cohort/legacy bridge flags, and refreshes caches; `GET /version-filter-options` is registry/disk derived; `GET /quality` summarizes SMS/general-agent quality labels by version (`prompt_rollout.py:178-418,476-668`). |

The API does more than expose admin-agent data: prompt promotion and Agent Court
playbook approval are direct human-admin mutation paths, deliberately outside
the model tool approval plane. That separation is appropriate, but auditors
should not summarize the service as a read-only dashboard.

### Admin-prefixed database tables

There are ten `admin_*` tables in `database/schema.sql`; their roles and key
columns are below. The six internal-agent state/evidence tables are strict
mode-RLS tables. The first four are global operational/configuration tables and
do not carry an organization/mode RLS key, which is reasonable for their role
but makes API/router protection their principal boundary.

| Table | Role and key columns |
| --- | --- |
| `admin_agent_github_webhook_events` | Global normalized GitHub App delivery journal: UUID PK, unique `delivery_id`, event/repository/PR/actor/SHAs/source URL, `raw_payload`, received time; no org/mode (`schema.sql:662-693`). |
| `admin_search_index_global_cursors` | Global search index checkpoint: UUID PK and unique `(namespace, entity_type, pipeline)`, with `last_indexed_at`/row cursor (`schema.sql:695-706`). |
| `admin_flags` | Global admin configuration: primary-key `name`, JSONB `value`, timestamp and `updated_by` user (`schema.sql:2328-2335`). |
| `admin_personal_access_tokens` | User-owned administrative token metadata: UUID PK, owner, unique owner/name, token hint/hash, expiry/revocation/audit columns (`schema.sql:2337-2357`). |
| `admin_agent_answer_feedback_events` | Mode-RLS answer-quality feedback: org/mode/run/conversation/item/optional turn, creator, category/note, artifact and ledger refs; cascades with source run/conversation/item (`schema.sql:4247-4283`). |
| `admin_saved_investigations` | Mode-RLS durable snapshot: org/mode plus nullable source run/conversation and saver, title/answer/state/confidence, denormalized source/artifact/evidence/ledger/task/coverage/usage JSON. Source FKs are `SET NULL`, intentionally retaining snapshots after source deletion (`schema.sql:4285-4345`). |
| `admin_tool_audit_events` | Mode-RLS controlled-tool ledger: UUID, run/org/mode, tool, status/timing, shaped payload, counts/raw-visible-compacted sizes/error/tool-call/backend PID; unique partial reservation `(run_id, tool_call_id)` (`schema.sql:4347-4386`). |
| `admin_artifacts` | Mode-RLS run-scoped durable result/artifact: org/mode/run, optional audit-event FK, type/version/title/summary/lifecycle/source/evidence/window/caveats/payload. Parent run deletion cascades; audit link becomes null (`schema.sql:4388-4429`). |
| `admin_investigation_executions` | Mode-RLS parent fan-out state: org/mode/parent run, optional ledger artifact, title/status/instructions, max worker/timeout/telemetry/timestamps (`schema.sql:4431-4467`). |
| `admin_investigation_tasks` | Mode-RLS child-target work state: execution + unique ordinal, target/allowed sources/tools, model/tier/status/verdict/confidence/output/error, timing and cost/token/tool counts, optional unique child run (`schema.sql:4469-4532`). |

The generic `phoebe_agent_runs`, conversations, turns/items and pending-event
tables supply the actual agent event history and are equally load-bearing, but
they are not `admin_*` tables; the admin rows attach to them by run or
conversation FK. The persistence design is therefore a thin, strongly scoped
admin layer on top of the shared event engine, rather than a separate agent
database.

## Safety and permissions posture

### Invariant assessment

| Invariant | Assessment | Evidence and qualification |
|---|---|---|
| Admin tools are separated from customer tools | **Verified** | Admin tools have a distinct `AdminAgentToolContext` (not `PhoebeToolContext`), static registry, controlled wrapper, and only mount after admin runtime verification (`admin_tool_contracts.py:55-67`, `admin_tool_registry.py:1-215`, `runtime_assembly.py:3750-3914`). The general runtime does not receive this registry; an explicit test covers that. |
| Tools are read-only by construction | **Partly verified / intentionally weakened** | Read definitions default to `read_only=True`; registry import-graph test rejects write-capable DB imports from read tool paths, and app reads use a transaction-local readonly role (`admin_tool_contracts.py:367-428`; `admin_tool_registry_test.py:1366-1403`; `admin_tool_controls.py:132-163`). But 22 registered tools are deliberately write-capable (enumerated below), and most of their scopes are audit-only rather than human-approved. |
| No free-form SQL | **Verified for the Admin Agent surface** | SQL is catalog/templated; raw statement input is not an Admin Agent tool contract. DB role/session and EXPLAIN cost gating add defense in depth (Domain 3). This does not make every external service query language-free: PostHog receives generated typed HogQL. |
| Readonly DB role is verified at connect | **Verified, with a narrow privilege check** | Source-router pool initialization rejects superusers, `BYPASSRLS`, and INSERT on `app.organizations`, then each query uses a readonly transaction and verified statement timeout (`admin_agent_sources/routing.py:301-402,443-459,496-524`). The in-process ORM path does `SET LOCAL ROLE admin_agent_readonly`, `SET LOCAL transaction_read_only=on`, and `SHOW transaction_read_only` (`admin_tool_controls.py:132-163`). The direct check tests one representative INSERT privilege, not every possible DDL/DML grant; transaction-readonly remains the immediate backstop. Bootstrap grants only SELECT, NOBYPASSRLS and a 10-second role timeout (`database/bootstrap.sql:193-223`). |
| Investigated content is untrusted evidence | **Partly verified** | The root prompt has markers/rule, wrappers neutralize an embedded closing marker, and high-risk text surfaces—Core email, Core API, trace excerpts, Logfire, MCP, Slack contract, subagent payloads—use it (`admin_tool_contracts.py:25-51`; `runtime_assembly.py:472-484`; examples `admin_core_email.py:548`, `admin_core_api.py:1128-1189`). It is not universally imposed by the central controlled-tool wrapper: a new source/tool can return an ordinary string unless its module wraps it. That is a maintainability/prompt-injection gap. |
| Admin authorization fails closed | **Verified** | All `/admin` routers inherit an authenticated-admin dependency (`routes/admin/__init__.py:163-182`), whose Redis cache falls back to DB on cache error/miss and denies non-admins (`dependencies/admin.py:31-83`). Before mounting tools the runtime re-queries `User.admin`; Slack additionally validates a configured internal workspace and internal email, both failing closed (`runtime_assembly.py:3114-3141,3237-3357,3771-3800`). Route and runtime deny tests exist (`agent_test.py:2289-2303`; `runtime_assembly_admin_test.py:2107-2125,2991-3038`). The cache’s allow decision is time-bounded rather than rechecked on every route request; runtime defense closes the important execution-time window. |
| Mode/sandbox isolation is carried through admin paths | **Mostly verified** | Run context passes conversation mode, readonly source clients set `app.mode` transaction-locally, and admin records/routes generally query with `mode_context` and mode predicates (`runtime_assembly.py:3979-4004`; `routing.py:572-584`; e.g. `agent_answer_feedback.py:198-300`, `agent_saved_investigations.py:818-939`). Cross-mode trace inspection deliberately searches both modes for explicit IDs; Agent Court has narrowly-scoped `no_mode_context()` reads. Those intentional bypasses deserve continuing review but are visible in source (`admin_agent_court_workflows.py:601,740,1204-1282`). |
| Every call is auditable and sensitive arguments/logs are redacted | **Strongly verified for controlled tools; limited on result PII** | Validation/denial, start, terminal success/failure/cancel paths all write `admin_tool_audit_events`; no active run means refuse to execute; duplicate tool-call start is atomically blocked (`admin_tool_controls.py:253-430,1192-1264,1312-1396`). Production success is withheld if durable audit persistence fails, while local/staging success is fail-open; failed/denied calls are logged best-effort (`admin_tool_controls.py:1364-1395`). Spans/audit retain shapes, not values, with sensitive-name redaction and depth/key/sample caps (`admin_tool_controls.py:206-225,1399-1448`). This does not uniformly redact sensitive *provider output* persisted in artifacts/audit-adjacent evidence; internal access and source-specific shaping are the intended boundary. |
| Provider resource visibility uses native permissions | **Verified by design; deployment scopes unverified** | Native clients use integration/API credentials and explicitly describe provider sharing/installation as the authorization boundary (Domain 5); Phoebe does not add a resource allowlist. The code cannot prove a deployed token’s least privilege. Secrets are configuration-only, use environment-specific `ADMIN_AGENT_*` names, and missing credentials report names rather than values (`admin_agent_sources/routing.py:1109-1299`; `admin_integration_migration_inventory.py:214-498`). |
| Capability promotion/release stages gate exposure | **Verified structurally** | Capability validation rejects missing explicit manifests, invalid feature flags, duplicate tools, and stale approval references at import/startup (`capability.py:747-832`). It validates declaration shape, not whether a write is appropriate in the real provider account. |

### The actual write surface

“Read-only” therefore means the default investigation plane, not a total
system property. Every registered non-readonly definition goes through the
same controlled validation/audit wrapper, but human approval is required only
for `PHOEBE_PRODUCTION_MUTATION` and `LIVE_CUSTOMER_SIDE_EFFECT`
(`admin_tool_contracts.py:103-134`):

| Policy | Registered write tools / operation groups | Human approval |
|---|---|---|
| `PHOEBE_PRODUCTION_MUTATION` | `admin_agent_automations`; `update_admin_organization_feature_flags` | **Yes** |
| `LIVE_CUSTOMER_SIDE_EFFECT` | `invite_admin_organization_user` (also queues invitation email) | **Yes** |
| `SANDBOX_FIXTURE_WRITE` | `create_or_resolve_general_agent_probe_org` | No, sandbox-only fixture lane |
| `ADMIN_INVESTIGATION_METADATA` | `report_missing_tool`; `run_general_agent_probe` | No (metadata/run creation) |
| `EXTERNAL_WORKFLOW_OUTPUT` | `set_call_recording_tags`; `post_admin_github_pr_review`; Linear workflow mutation; apply Notion write; `run_agent_court_judge`, `queue_agent_court_justice`, `preview_head_justice_digest`; code-sandbox create/run/write/export/destroy/reclaim; Orchard draft-PR task; seven Slack operation groups—account-health post, post/reply/upload/schedule/update/delete | **No: audit-only**, despite real external effects |

The table is derived from the registry’s explicit definitions:
`admin_slack_registry.py:139-248`, `admin_github_pr_review.py:810-842`,
`admin_linear_workflows.py:795-827`, `admin_notion_workflows.py:760-790`,
`admin_call_transcripts.py:1347-1366`, `admin_code_sandbox.py:1027-1057`,
`admin_orchard_code_change.py:780-810`, `admin_agent_court_workflows.py:2314-2372`,
`admin_organization_membership.py:517-544`,
`admin_org_feature_flags.py:571-601`, `admin_agent_automations.py:1481-1510`,
`admin_general_agent_probe*.py:645-668,2332-2362`, and
`admin_missing_tool_reports.py:293-314`.

The distinction is intentional, not accidental: the GitHub write prompt
explicitly says it is audit-only and forbids merge, push, and workflow edits
(`admin_github_pr_review.py:810-824`). It is nevertheless a material policy
choice: Linear/Notion/Slack/GitHub comments, Core tags, sandbox commands and
Orchard dispatches can change external state without an approver. Preview,
exact-input validation, channel/repository restrictions, provider token scope,
and durable audit reduce risk; they are not equivalent to consent.

### Other control details and residual gaps

The controlled wrapper creates a durable `RESERVED` audit event before tool
execution, associates database backend PID before SQL, validates typed output,
persists artifacts, caps visible payloads, and finalizes one audit row. It
also refuses production data if audit persistence fails
(`admin_tool_controls.py:253-548,1192-1289,1364-1396`). This is an
excellent, centralized choke point.

Two important limitations remain. First, output caps are opt-in on each tool
definition (`compact_result_output`) and defaults are `None`; not every
read-tool result has a central hard byte bound (`admin_tool_contracts.py:367-390`).
Second, the import guard is static Python-AST coverage, not a runtime security
monitor; it detects the sanctioned source package’s ordinary import paths but
cannot prove a future dynamic/indirect provider call is read-only. The test
suite does cover marker escaping, result caps failing closed, non-admin route
denials, non-admin runtime owners, Slack workspace denial, readonly write
failure, and production audit persistence failure (`admin_tools_test.py:1415-25,2673,3796,3872-4123`; `runtime_assembly_admin_test.py:2107-2125`). I did
not find an end-to-end adversarial suite that systematically tries every
audit-only external write under a malicious prompt or verifies deployed token
scopes; treat that as untested.

## Gaps and risks

### High

1. **`run_admin_python_snippet` is a resource-limited subprocess, not a
   security boundary.** It can use the worker account's reachable filesystem
   and standard-library networking; no network namespace, seccomp, chroot or
   VM exists. This conflicts with the intuitive name “sandbox” and is severe
   if a prompt, artifact, or future tool lets untrusted code reach the lane
   (`libraries/python/phoebe_admin_agent/admin_python_snippet.py:2479-2742,3327-3359`).

2. **The exe.dev code sandbox grants arbitrary shell commands and lacks
   per-run ownership enforcement.** Helper path filtering is bypassable through
   the shell; command execution inherits a VM environment; global tagging
   permits listing/operating an otherwise-known admin sandbox across runs; and
   expiry reclamation is opt-in. “Do not exfiltrate” is prompt doctrine, not a
   technical egress control (`admin_code_sandbox.py:297-330,472-743,589-673`;
   `admin_code_sandbox_client.py:387-410,741-789`).

3. **“Read-only” has a large, no-human-approval external-write exception.**
   Slack messages/deletes/uploads, Core recording tags, Linear/Notion changes,
   GitHub reviews/comments, code-sandbox lifecycle/commands and Orchard task
   dispatches are real side effects classified as `EXTERNAL_WORKFLOW_OUTPUT`.
   They are schema-validated and audited but do not enter the approval queue
   (`admin_tool_contracts.py:103-134`; write inventory in the Safety section).
   The policy should be re-evaluated per operation, particularly destructive
   Slack actions, external record mutation and remote command execution.

4. **Result/data-retention boundaries are incomplete.** Generic typed database
   queries default to no row/byte/cell limit, and tool result caps are optional.
   Full results become database artifacts; no generic artifact TTL/purge was
   found. A low-cost query can therefore produce a large durable sensitive
   payload (`admin_database_query.py:43-56,2361-2444`; `admin_artifacts.py:733-765`).

5. **Internal run economics are detect-only rather than bounded.** Internal
   and subagent runs deliberately have unbounded iterations/continuations;
   fan-out has only a per-execution eight-worker cap; the $10 Slack alert is
   after-the-fact. A malformed tool/model interaction can use large time and
   budget before recovery, cancellation, or an operator intervenes
   (`process_agent_run.py:68-80`; `subagent_payloads.py:45-80`; `lifecycle.py:703-1005`).

### Medium

6. **Untrusted-evidence wrapping is a convention, not a mandatory wrapper
   invariant.** Strong source modules use it, but the central controlled-tool
   layer does not enforce it for all model-visible strings. A new or less
   careful integration can reintroduce instruction-following text as ordinary
   evidence (`admin_tool_contracts.py:25-51`; `admin_tool_controls.py:253-548`).

7. **Production integration authorization is unproven.** The design correctly
   delegates resource visibility to GitHub/Linear/Notion/etc. credentials, but
   source cannot establish deployed OAuth/app scopes, repository/channel
   restrictions, credential rotation, or tenancy boundaries. Native
   permissions should be continuously tested and reviewed, not assumed from
   wrapper code (`admin_agent_sources/routing.py:1109-1299`).

8. **Evals have a live-model and hostile-path gap.** The best investigation
   tests are skipped without `ANTHROPIC_API_KEY`, all tools are stubbed, and
   the runner caps itself at 12 iterations. No golden e2e set covers adversarial
   evidence, approval interception, external mutation families, code-exec
   isolation, cross-run sandbox identity, provider scope, or retention
   (`admin_investigation_eval_test.py:49-78`; `admin_investigation_runner.py:634-654`).

9. **Cross-mode/no-RLS reads are intentionally powerful.** Saved investigation
   discovery, missing-tool/telemetry/automation dashboards, Agent Court and
   trace utilities use cross-mode or `no_rls` lookups. They retain the inherited
   admin route gate and commonly re-enter the object mode, but a future
   alternate mount or missing object predicate would make these high-value
   cross-tenant read paths (`agent_saved_investigations.py:816-949`,
   `agent_court.py:774-841`, `agent_tool_telemetry.py:746-809`).

10. **Some declared safety is weaker than its implementation label.** The
    DB-source verifier samples privilege facts rather than proving no inherited
    DDL/DML grants; AST import guards do not catch dynamic/indirect provider
    calls; output/redaction policy does not guarantee provider-result PII
    minimization; draft/no-merge in Orchard depends on a separate executor
    (`admin_agent_sources/routing.py:301-402`; `admin_tool_registry_test.py:1366-1403`; `admin_orchard_code_change.py:719-724`).

### Low / product-operability

11. **Documentation drift remains around Snowflake.** The implementation has
    removed it, while older planning/context documents still describe an active
    Snowflake catalog. This can cause an operator to ask for a capability that
    does not exist (`docs/notes/plans/pho_13589_admin_database_curated_queries_execplan.md:14-32`).

12. **Artifact/UI capability drift is visible.** The visualization schema
    accepts scatter, histogram and graph kinds that the web renderer does not
    render specially; valid outputs degrade to a fallback (`admin_visualization_artifacts.py:20-245`; `artifacts/visualization.tsx:32-42`).

## Half-finished arcs and recommended next work

1. **Make execution boundaries real before expanding autonomy (P0).** Remove
   arbitrary worker-process Python or move it to a network-denied, filesystem-
   scoped sandbox. Bind remote sandbox operations to `(organization, mode,
   run, caller)` rather than a global tag; enforce TTL destruction server-side;
   add outbound network policy and provider credential isolation. Add negative
   tests that prove one run cannot read, command, list, or destroy another's
   sandbox.

2. **Reclassify side effects and require explicit approval where intent matters
   (P0).** Split `EXTERNAL_WORKFLOW_OUTPUT` by action: destructive Slack,
   provider mutation, remote command, and external-task dispatch should have
   distinct policy/audit semantics, confirmation previews, idempotency keys and
   approval requirements. Keep the GitHub no-merge/push guard, but verify it at
   the executor rather than only in prompt/payload metadata.

3. **Set global output, cost and retention ceilings (P0).** Give every
   model-visible tool a conservative byte/cell/row envelope and artifact spill
   contract; make database caller limits mandatory; cap Internal/subagent
   iterations, calls, fan-out, wall time and dollar budget with a checkpointed
   “needs continuation” state; set retention class/expiry/deletion jobs for
   artifacts, especially email/trace/full-result/image and sandbox output.

4. **Centralize evidence and PII policy (P1).** Move untrusted-evidence
   marking into a mandatory result normalizer (or require an explicit typed
   safe/unsafe field) so registry validation fails a tool that exposes free
   provider text without a treatment. Couple it with source-specific data
   minimization and artifact-access/retention rules rather than relying on
   prompt instructions.

5. **Turn the excellent golden-eval foundation into release evidence (P1).**
   Require an approved live-model/replay lane, track case pass rate and
   flakiness, and add suites for prompt injection, every mutation scope,
   approval denial/timeout, payload/output abuse, cross-mode handling, sandbox
   ownership/egress and provider read-only behavior. A capability should not
   advance from `internal_only` on manifest declarations alone.

6. **Finish the operational consistency work (P1).** Reconcile the Snowflake
   plans with actual removed functionality; make artifact chart/visualization
   schemas match renderers; expose a safe durable artifact export path instead
   of reconstructing bytes for Slack; and make external integrations' deployed
   scopes observable on the source-health/capability page.

7. **Harden cross-mode administrative read paths (P2).** Document each
   `no_rls` use with a required object-type, admin-auth and mode-reentry check;
   add a static/routing test that every `get_no_rls_db_session` agent endpoint
   is mounted only under the admin parent; and audit global `admin_*` tables
   separately from mode-RLS investigation data.

The broad product arc is clear: Phoebe has built a sophisticated internal
agent operating system with credible read/investigation mechanics, then added
write and execution lanes faster than their enforcement boundaries matured.
The next work should consolidate the latter—not add more tools—so the strong
registry, audit, artifact and capability-promotion foundations remain
trustworthy as the agent becomes more autonomous.
