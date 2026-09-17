---
type: reference
tags: [phoebe, admin-v3, design]
created: 2026-08-19
updated: 2026-08-19
---

# PHO-16438: rung-12 domain-family design

This is a design proposal. It does not edit the v3 ledger.

The source baseline is `origin/main`. The required sources were:

- `libraries/python/phoebe_admin_agent_v3/PORT_LEDGER.md`
- `libraries/python/phoebe_admin_agent_v3/port_ledger.json`
- `libraries/python/phoebe_admin_agent_v3/ORGANIZATION.md`
- `libraries/python/phoebe_admin_agent/admin_runtime_characterization_digest.json`
- the v2 implementations under `libraries/python/phoebe_admin_agent/`

The v2 digest has 190 ledger tool names. Its top-level admin mode has 188.
The two names that appear only in other live modes are the two already marked
Slack-deferred in the ledger. The 15 approval-required names remain the d4
source of truth. Every other legacy name below keeps the digest's
`audit_only` or read-only class unless Henry approves a later policy change.

## Design rules

The three family builds use the core v3 doors:

- `query` is the read door. It accepts generated catalog expressions and
  named provider adapters. It does not accept arbitrary SQL or provider
  commands.
- `write` is the mutation door. It accepts a typed operation union, explicit
  target and environment, `preview` or `execute`, a reason, and an
  idempotency key.
- `codebase` is the one extra model-visible entry for source intelligence.
  Its intent union is smaller than the old codebase surface and remains
  read-only.

No family tool is proposed as `carried`. Each old model-visible name either
merges into a new door or leaves this ladder. Low-level behavior remains in
skill-local adapters. A carried disposition would preserve the v2 sprawl and
needs a separate justification.

The proposed `descoped` entries mean that the v3 surface will not provide the
legacy capability in this ladder. The ledger PR must add the required
`reason`, `decided_in`, `approved_by: henry`, and `absent` divergence. The two
existing Slack `deferred` entries remain deferred. Other Slack writes below
also stay out of the first ladder because d3 defers Slack functionality.

## family a: notes, pages, and settings

### consolidated tool surface

Expose only the core `query` and `write` doors.

`query` mounts skill-local adapters for these read intents:

- `admin_note.search`, `admin_note.read`, and `admin_note.revisions`;
- `admin_page.list` and `admin_page.read`;
- `organization_settings.read` and `organization_settings.location`;
- `organization_feature_flags.catalog`, `organization_feature_flags.read`,
  and `organization_feature_flags.rollout`;
- `phone_routing.read`;
- `organization_territory.list`, `organization_territory.resolve`, and
  `organization_territory.resolve_batch`;
- `organization_membership.inspect`.

`search_notes` and `search_admin_notes` use one note search adapter. The
adapter retains the v2 scope, tag, volatility, recency, and mode filters. It
returns note metadata first. A second read loads the body or revisions.

Page metadata stays read-only. Page Snowflake schema lookup belongs to family
c and uses its schema adapter. This avoids two page-specific schema tools.

`write` mounts these operation ids:

- `admin_note.create`, `admin_note.update`, `admin_note.retire`, and
  `admin_note.confirm`;
- `admin_page.version.create`, `admin_page.version.activate`,
  `admin_page.refresh`, and `admin_page.refresh_schedule.configure`;
- `organization_settings.update`;
- `organization_feature_flags.update`, with `mode: preview` replacing the
  separate preview tool;
- `phone_routing.forwarding.add`, `phone_routing.forwarding.update`,
  `phone_routing.destination.add`, and `phone_routing.destination.update`;
- `organization_territory.upsert`;
- `organization_membership.invite`.

Each operation has a frozen input model. In particular, settings updates use a
family-specific field union, not the v2 open-ended `updates` dictionary.
Feature flags accept only canonical boolean keys. Page activation and refresh
retain compare-and-swap ids. Territory upsert retains complete normalized
city and postal-prefix rules.

### per-ledger-entry disposition proposal

#### notes

- `search_admin_notes` — redesigned: merge semantic and full-text note search into `admin_note.search`.
- `search_notes` — redesigned: use the same bounded note index adapter instead of a second search door.
- `read_admin_note` — redesigned: expose `admin_note.read` through `query` with explicit note scope.
- `read_admin_note_revisions` — redesigned: make revision history a typed `admin_note.revisions` query intent.
- `write_admin_note` — redesigned: map to `admin_note.create` in the write registry.
- `update_admin_note` — redesigned: map to `admin_note.update` and retain revision creation and evidence refs.
- `retire_admin_note` — redesigned: map to `admin_note.retire` with soft-retire semantics.
- `confirm_admin_note` — redesigned: map to `admin_note.confirm` with a typed evidence ref.

#### pages

- `list_admin_pages` — redesigned: merge into `admin_page.list` metadata query.
- `read_admin_page` — redesigned: merge into `admin_page.read` with bounded version and snapshot history.
- `create_admin_page_version_from_sandbox` — redesigned: map to `admin_page.version.create` and keep the run-owned sandbox contract.
- `activate_admin_page_version` — redesigned: map to `admin_page.version.activate` with exact version, snapshot, and expected-active ids.
- `refresh_admin_page_data` — redesigned: map to `admin_page.refresh` and keep pinned dataset and compare-and-swap rules.
- `configure_admin_page_refresh_schedule` — redesigned: map to `admin_page.refresh_schedule.configure` with revision checks.

#### settings, flags, membership, routing, and territories

- `find_admin_settings_location` — redesigned: make it `organization_settings.location` over the settings surface map.
- `get_admin_organization_settings` — redesigned: make it `organization_settings.read` with typed family selectors.
- `update_admin_organization_settings` — redesigned: map to `organization_settings.update` with per-family fields.
- `get_admin_organization_feature_flags` — redesigned: merge catalog search and organization reads into `organization_feature_flags.read`.
- `search_feature_catalog` — redesigned: fold known-flag catalog search into `organization_feature_flags.catalog`.
- `inspect_admin_feature_flag_rollout` — redesigned: expose bounded rollout metadata as `organization_feature_flags.rollout`.
- `preview_admin_feature_flag_updates` — redesigned: use `organization_feature_flags.update` with `mode: preview`; remove the duplicate preview name.
- `update_admin_organization_feature_flags` — redesigned: map to the typed boolean `organization_feature_flags.update` operation.
- `inspect_admin_organization_membership` — redesigned: expose members and pending invitations through `organization_membership.inspect`.
- `invite_admin_organization_user` — redesigned: map to `organization_membership.invite` with an explicit target organization.
- `get_admin_phone_routing` — redesigned: make it the `phone_routing.read` query intent.
- `add_admin_phone_forwarding` — redesigned: map to `phone_routing.forwarding.add`.
- `update_admin_phone_forwarding` — redesigned: map to `phone_routing.forwarding.update`.
- `add_admin_call_routing_destination` — redesigned: map to `phone_routing.destination.add`.
- `update_admin_call_routing_destination` — redesigned: map to `phone_routing.destination.update`.
- `list_org_territories` — redesigned: make it `organization_territory.list`.
- `resolve_org_territory` — redesigned: make it `organization_territory.resolve`.
- `resolve_org_territories` — redesigned: make it `organization_territory.resolve_batch` with the existing batch cap.
- `upsert_org_territory` — redesigned: map to `organization_territory.upsert` and retain normalized replacement semantics.

No entry in family a is carried. The v2 sources show separate note read and
mutation groups, five page groups, separate settings and feature-flag groups,
and separate routing and territory groups. Those are implementation modules,
not useful model concepts.

### write-registry split

| operation id | legacy entries | d4 class | registry rule |
| --- | --- | --- | --- |
| `admin_note.create` | `write_admin_note` | audit-only | durable note, evidence refs, idempotent create |
| `admin_note.update` | `update_admin_note` | audit-only | lock current version, write revision, then update |
| `admin_note.retire` | `retire_admin_note` | audit-only | soft-hide only; retain id-based reads |
| `admin_note.confirm` | `confirm_admin_note` | audit-only | record note version and evidence ref |
| `admin_page.version.create` | `create_admin_page_version_from_sandbox` | audit-only | create inactive immutable candidate |
| `admin_page.version.activate` | `activate_admin_page_version` | approval-required | exact pair and expected-active compare-and-swap |
| `admin_page.refresh` | `refresh_admin_page_data` | approval-required | refresh only the pinned active contract |
| `admin_page.refresh_schedule.configure` | `configure_admin_page_refresh_schedule` | approval-required | change automatic refresh policy |
| `organization_settings.update` | `update_admin_organization_settings` | approval-required | validate a closed settings-family field union |
| `organization_feature_flags.update` | `preview_admin_feature_flag_updates`, `update_admin_organization_feature_flags` | approval-required for execute; audit-only for preview | one operation with `preview` and `execute` modes |
| `phone_routing.forwarding.add` | `add_admin_phone_forwarding` | approval-required | add forwarding metadata for one organization |
| `phone_routing.forwarding.update` | `update_admin_phone_forwarding` | approval-required | update one explicit forwarding record |
| `phone_routing.destination.add` | `add_admin_call_routing_destination` | approval-required | add one routing destination |
| `phone_routing.destination.update` | `update_admin_call_routing_destination` | approval-required | update one explicit routing destination |
| `organization_territory.upsert` | `upsert_org_territory` | approval-required | replace one normalized territory definition |
| `organization_membership.invite` | `invite_admin_organization_user` | approval-required | create one pending invitation and email job |

All other family-a names are skill-local query adapters. A pending approval
must not open a transaction. Repeating an idempotency key returns the prior
outcome.

### Henry approval items and risk

Each item below needs explicit d11 approval before its ledger disposition
merges.

- Note read consolidation: `search_admin_notes`, `search_notes`,
  `read_admin_note`, and `read_admin_note_revisions` become one query family.
  Risk: prompt routing may lose the old distinction between snippets, bodies,
  and revisions.
- Note mutation consolidation: the four `admin_note.*` operations replace
  four model tools. Risk: an incorrect union branch could overwrite or retire
  durable internal memory.
- Page read consolidation: `list_admin_pages` and `read_admin_page` become
  one query contract. Risk: a smaller result could hide version or freshness
  state needed for rollback decisions.
- Page write consolidation: the four `admin_page.*` operations replace four
  page tools. Risk: activation, refresh, and schedule changes have different
  user-visible effects and approval prompts.
- Settings and flag consolidation: all settings and feature-flag reads and
  writes use typed selectors and one update operation. Risk: an incorrect
  field or flag mapping can change an organization runtime.
- Routing consolidation: the four routing operations replace separate
  forwarding and destination tools. Risk: a wrong destination or forwarding
  record can misroute calls.
- Territory consolidation: list and resolve become query intents, while
  upsert becomes one write operation. Risk: a broad rule can change caregiver
  or client assignment behavior.
- Membership consolidation: inspect and invite use one typed domain contract.
  Risk: a wrong organization or role can grant access or send an invitation.

### build sequencing

- The family-a read adapters can build in parallel with family-b and family-c
  read adapters after rung 4 `query` is stable.
- Note writes and `organization_settings.update` depend on rung 5 `write` and
  are the first family-a mutation slice allowed by d3.
- Feature-flag, routing, territory, membership, and page activation writes
  depend on rung 5 plus separate Henry approval. They are not first-slice
  writes.
- `admin_page.version.create` also depends on the rung-6 workspace and bash
  evidence contract because its inputs are run-owned sandbox files.
- Page reads do not depend on family-b or family-c. Page schema reads use the
  family-c schema adapter and therefore share its rung-4 dependency.

## family b: call analysis and quality

### consolidated tool surface

Expose the core `query` and `write` doors. Do not expose separate transcript,
analysis, account-health, or quality-review tools.

The call query adapter uses a typed `source: core_call_recordings` selector
with three intents: `index_search`, `transcript_read`, and
`transcript_search`. It preserves the Core API boundary, paging, transcript
match caps, untrusted-evidence wrapping, and rate limits. It must reject a
caregiver voice-call query that belongs to a different source.

The account-health query adapter uses named catalog queries for metrics,
batch rollups, and the PostHog account-health user view. It requires explicit
organization ids and local-time windows. The quality adapter returns bounded
review findings and evidence refs. It does not post to Slack.

The write registry exposes:

- `call_recording.tags.replace`;
- `call_analysis.submit` and `call_analysis.update`;
- `account_health.review.mark`;
- `account_health.followup.publish`, which is not mounted while Slack is
  deferred;
- `quality_review.publish`, which is also not mounted while Slack is
  deferred.

`submit_call_analysis` keeps its typed call taxonomy, narrative, unanswered
questions, caregiver outcome fields, failure modes, and follow-up references.
It returns follow-up intents. It does not silently execute a Linear or Slack
write from inside analysis submission.

### per-ledger-entry disposition proposal

#### call recordings and analysis

- `search_call_recording_index` — redesigned: map to `query` intent `index_search` with the existing Core filters and limit.
- `read_call_recording_transcript` — redesigned: map to `query` intent `transcript_read` with bounded pages and evidence refs.
- `search_call_recording_transcript` — redesigned: map to `query` intent `transcript_search` with bounded context lines and matches.
- `set_call_recording_tags` — redesigned: map to `call_recording.tags.replace` and require the full desired tag set.
- `submit_call_analysis` — redesigned: map to `call_analysis.submit` with the versioned typed analysis contract.
- `update_call_analysis` — redesigned: map to `call_analysis.update` with explicit recording and expected revision.

#### quality and account health

- `get_admin_account_health_metrics` — redesigned: merge into the bounded `account_health.metrics` query intent.
- `get_admin_account_health_rollups` — redesigned: merge into the same account-health query family with batch limits.
- `get_admin_posthog_account_health_users` — redesigned: keep a skill-local PostHog adapter behind the account-health query source.
- `review_admin_tool_quality` — redesigned: return bounded quality findings through the quality query adapter.
- `mark_account_reviewed` — redesigned: map to `account_health.review.mark` with one explicit organization and reason.
- `post_account_health_threads_to_slack` — deferred: preserve the behavior in the ledger, but do not mount it while d3 defers Slack.
- `post_daily_tool_quality_review_to_slack` — deferred: retain the existing ledger entry and its `absent` divergence while Slack remains deferred.

### write-registry split

| operation id | legacy entries | d4 class | registry rule |
| --- | --- | --- | --- |
| `call_recording.tags.replace` | `set_call_recording_tags` | audit-only | require an explicit call id and complete replacement tag set |
| `call_analysis.submit` | `submit_call_analysis` | audit-only | validate the versioned analysis union before recording outcome |
| `call_analysis.update` | `update_call_analysis` | audit-only | require explicit recording id and expected analysis revision |
| `account_health.review.mark` | `mark_account_reviewed` | audit-only | live-only marker with organization-local review date |
| `account_health.followup.publish` | `post_account_health_threads_to_slack` | audit-only in v2; deferred by d3 | keep out of the mounted registry until Slack is approved |
| `quality_review.publish` | `post_daily_tool_quality_review_to_slack` | audit-only in v2; deferred by d3 | keep the exact scheduled digest behavior in the ledger |

The quality and account-health reads remain skill-local adapters. They do not
become write operations merely because they produce a recommendation.

### Henry approval items and risk

- Call query consolidation: the three recording read tools become one typed
  query source. Risk: mixing internal meeting recordings with caregiver voice
  calls could expose the wrong organization data.
- Tag mutation redesign: `set_call_recording_tags` becomes
  `call_recording.tags.replace`. Risk: a replacement input can erase existing
  tags if the read-before-write contract fails.
- Analysis submission redesign: `submit_call_analysis` becomes a registry
  operation. Risk: a malformed or stale analysis can feed customer-facing
  summaries and follow-up workflows.
- Analysis correction redesign: `update_call_analysis` becomes a registry
  operation. Risk: a correction can overwrite a reviewed analysis without an
  expected revision.
- Account-health read consolidation: metrics, rollups, and PostHog users use
  one query family. Risk: a wrong time zone or organization scope changes
  health conclusions.
- Review marker redesign: `mark_account_reviewed` becomes a write operation.
  Risk: an incorrect marker can suppress needed follow-up work.
- Slack follow-up descope: the two account-health and quality Slack writes
  stay deferred. Risk: operators lose automated Slack visibility until the
  later Slack bundle is built.
- Quality-review read redesign: `review_admin_tool_quality` moves behind the
  query door. Risk: a smaller evidence packet could hide a recurring tool
  failure.

### build sequencing

- Call-recording and account-health query adapters can build in parallel with
  family-a and family-c query adapters after rung 4.
- The call analysis contract, tag write, correction write, and review marker
  depend on rung 5 `write`.
- Call analysis must not import the family-c Linear implementation. A future
  follow-up intent can target a family-c write operation by stable operation
  id, or remain a receipt until that operation is approved.
- Slack follow-ups wait for the later Slack surface. They are not blockers for
  the non-Slack quality and analysis build.
- The account-health batch implementation can proceed after query contracts,
  but its organization-local time-window tests should land before any
  follow-up write is enabled.

## family c: codebase and external research adapters

### consolidated tool surface

Use two model-visible entries:

1. `query` for provider-backed research and schema reads.
2. `codebase` for immutable source intelligence.

The provider query adapter has a typed `provider` union: `github`, `linear`,
`notion`, `calendar`, `posthog`, `datadog`, `logfire`, `braintrust`,
`intercom`, and `snowflake_schema`. Each provider exposes named catalog
actions, bounded output, provider-specific authorization, and normalized
evidence refs. Incident.io is not included because its current on-call lookup
is a single-consumer incident relic.

The `codebase` input has one `intent` union:

`search`, `symbols`, `references`, `relationships`, `map`, `outline`,
`source_range`, `localize`, `runtime_handoff`, `work_item`, `investigate`,
`evidence_review`, and `knowledge`.

It accepts a registered repository, optional branch, path filters, a bounded
limit, and narrow source ranges. It does not accept an arbitrary checkout
root in production, a force-refresh command, or write requests. The old
`inspect_codebase_wiki` broad input becomes the adapter implementation, not a
second model entry.

External writes use the core `write` door. They are not hidden inside a
provider query. The first ladder does not mount them, because d3 allows notes
and settings writes first.

### per-ledger-entry disposition proposal

#### GitHub

- `compare_admin_github_refs` — redesigned: map to `query(provider=github, action=compare_refs)`.
- `get_admin_github_file` — redesigned: map to bounded repository file read with a commit or branch pin.
- `inspect_admin_github_commit` — redesigned: map to `query(provider=github, action=inspect_commit)`.
- `inspect_admin_github_pull_request` — redesigned: map to `query(provider=github, action=inspect_pull_request)`.
- `inspect_admin_github_workflow_run` — redesigned: map to `query(provider=github, action=inspect_workflow_run)`.
- `list_admin_github_branches` — redesigned: map to the bounded `github.branches.list` adapter.
- `list_admin_github_commits` — redesigned: map to the bounded `github.commits.list` adapter.
- `list_admin_github_repositories` — redesigned: map to the bounded `github.repositories.list` adapter.
- `list_admin_github_tags` — redesigned: map to the bounded `github.tags.list` adapter.
- `list_admin_github_workflow_runs` — redesigned: map to the bounded `github.workflow_runs.list` adapter.
- `search_admin_github_pull_requests` — redesigned: map to `github.pull_requests.search` with provider pagination hidden.
- `post_admin_github_pr_review` — redesigned: map to `external.github.pull_request_review` in the write registry; do not mount in the first ladder.

#### Linear

- `search_admin_linear_issues` — redesigned: map to `linear.issues.search` with a typed filter expression.
- `inspect_admin_linear_issue` — redesigned: map to `linear.issue.read`.
- `inspect_admin_linear_document` — redesigned: map to `linear.document.read`.
- `apply_linear_workflow_mutation` — redesigned: map to `external.linear.workflow_mutate` in the write registry; do not execute from a query adapter.

#### Notion

- `search_admin_notion` — redesigned: map to `notion.search` with bounded page metadata and snippets.
- `inspect_admin_notion_page` — redesigned: map to `notion.page.read`.
- `preview_admin_notion_write` — redesigned: map to `external.notion.write` with `mode: preview`.
- `apply_admin_notion_write` — redesigned: map to `external.notion.write` with `mode: execute`; do not mount in the first ladder.

#### Calendar

- `list_admin_calendars` — redesigned: map to `calendar.calendars.list`.
- `list_admin_calendar_events` — redesigned: map to `calendar.events.list` with an explicit time window.
- `inspect_admin_calendar_event` — redesigned: map to `calendar.event.read`.

#### PostHog, Datadog, Logfire, Braintrust, Intercom, and Snowflake schema

- `investigate_posthog_analytics` — redesigned: map to a bounded `posthog.analytics` query adapter; do not expose provider query syntax.
- `investigate_datadog_alert` — redesigned: map to `datadog.alert` evidence lookup with a fixed time window.
- `investigate_logfire_records` — redesigned: map to `logfire.records` with typed filters and redacted provider errors.
- `investigate_braintrust_evals` — redesigned: map to `braintrust.evals` with bounded experiment and score filters.
- `get_core_intercom_conversation` — redesigned: map to `intercom.conversation.read` with explicit conversation identity.
- `get_current_incident_io_on_call` — descoped: remove the single-consumer on-call lookup from the first v3 surface.
- `describe_admin_page_snowflake_schema` — redesigned: merge into `snowflake_schema.describe`; page code does not own a second schema tool.
- `describe_table` — redesigned: merge into the same `snowflake_schema.describe` action with a typed relation name.
- `search_schema` — redesigned: map to `snowflake_schema.search` and keep it read-only.

#### Codebase intelligence

- `find_codebase_references` — redesigned: map to `codebase` intent `references`.
- `inspect_codebase_map` — redesigned: map to `codebase` intent `map`.
- `inspect_codebase_relationships` — redesigned: map to `codebase` intent `relationships`.
- `inspect_codebase_wiki` — redesigned: map to `codebase` intent `search`, `structure`, or `architecture`.
- `investigate_codebase` — redesigned: map to `codebase` intent `investigate` with deterministic evidence first.
- `localize_codebase_issue` — redesigned: map to `codebase` intent `localize`.
- `localize_runtime_evidence_to_code` — redesigned: map to `codebase` intent `runtime_handoff`.
- `map_admin_work_item_to_codebase` — redesigned: map to `codebase` intent `work_item`.
- `outline_codebase_file` — redesigned: map to `codebase` intent `outline`.
- `read_codebase_source_range` — redesigned: map to `codebase` intent `source_range` with narrow line bounds.
- `review_codebase_evidence` — redesigned: map to `codebase` intent `evidence_review` over a bounded evidence packet.
- `search_codebase_knowledge` — redesigned: map to `codebase` intent `knowledge`.
- `search_codebase_source` — redesigned: map to `codebase` intent `search` with source-only filtering.
- `search_codebase_symbols` — redesigned: map to `codebase` intent `symbols`.
- `compare_admin_agent_source_health` — descoped: remove the single-consumer source-health comparison report; use codebase evidence and query receipts instead.

The database query trio `query_admin_database`,
`run_admin_database_query`, and `list_admin_database_queries` is not a family-c
surface. Rung 4 owns that behavior through the catalog-backed `query` door.
The C design owns only schema discovery: `describe_table`, `search_schema`,
and the page-schema name above.

### write-registry split

| operation id | legacy entries | d4 class | registry rule |
| --- | --- | --- | --- |
| `external.github.pull_request_review` | `post_admin_github_pr_review` | audit-only | require an exact repository, pull request, review body, and idempotency key |
| `external.linear.workflow_mutate` | `apply_linear_workflow_mutation` | audit-only | accept only named workflow actions and bounded issue ids |
| `external.notion.write` | `preview_admin_notion_write`, `apply_admin_notion_write` | audit-only | one typed operation with `preview` and `execute`; no free-form provider commands |

All C read entries are skill-local adapters behind `query` or `codebase`.
Provider adapters own credentials, timeout, pagination, redaction, and
source evidence. The model sees normalized columns or evidence refs.

### Henry approval items and risk

- Provider-read consolidation: the GitHub, Linear, Notion, calendar, PostHog,
  Datadog, Logfire, Braintrust, Intercom, and schema names become one query
  surface. Risk: provider-specific scope or freshness warnings may disappear
  during normalization.
- Codebase consolidation: the codebase names become one intent union. Risk: a
  broad intent schema can route a source question to synthesis instead of a
  deterministic read.
- GitHub review redesign: `post_admin_github_pr_review` becomes an external
  write operation. Risk: it publishes text to a third-party repository.
- Linear mutation redesign: `apply_linear_workflow_mutation` becomes an
  external write operation. Risk: it changes issue state or ownership.
- Notion write redesign: preview and execute share one operation. Risk: a
  preview/execute mismatch can change a shared knowledge page.
- Incident.io descope: `get_current_incident_io_on_call` leaves the admin
  surface. Risk: an operator may not find current on-call ownership from the
  admin agent.
- Source-health descope: `compare_admin_agent_source_health` leaves the admin
  surface. Risk: a specialized stale-source warning is no longer automatic.

### build sequencing

- Provider read adapters can build in parallel with family-a and family-b
  query adapters after rung 4. Each provider needs an isolated contract test.
- The codebase entry can build in parallel after the evidence and workspace
  contracts are stable. It does not depend on rung 5 unless a future code
  change request is added.
- GitHub, Linear, and Notion write operations depend on rung 5 `write` and
  explicit Henry approval. They are not first-slice writes.
- The C read build must not import family-b call-analysis code. Call analysis
  may emit a stable external follow-up operation id, but execution belongs to
  the write registry.
- `snowflake_schema.describe` depends on rung 4 query validation and the
  provider authorization adapter. It does not depend on page version writes.
- No generic MCP adapter is a dependency. A later provider addition needs a
  named allowlist, owner, stable schema, and ledger entry.

## Henry decision list

**APPROVED 2026-09-14 (Henry, via phoebe orch): all 15 items per orchestrator recommendations** — 13 mapped-but-unmounted (mounting needs a separate ask), 10/14/15 descoped, 6 stays approval-required, 7 must keep the call-source distinction explicit, 11 must preserve per-provider auth semantics. Build lanes spawned: PHO-16438-FAMA/FAMB/FAMC.


The following decisions need explicit Henry approval before the build PRs flip
ledger entries. Each line names the affected legacy entries and the risk.

1. **family-a read door.** Consolidate `search_admin_notes`, `search_notes`,
   `read_admin_note`, `read_admin_note_revisions`, `list_admin_pages`,
   `read_admin_page`, settings reads, flag reads, membership inspection,
   routing reads, and territory reads behind `query`. Risk: prompt behavior
   and evidence detail can change when many tools share one result contract.
2. **family-a note writes.** Map `write_admin_note`, `update_admin_note`,
   `retire_admin_note`, and `confirm_admin_note` to the four `admin_note.*`
   operations. Risk: durable memory can be overwritten, hidden, or confirmed
   against the wrong evidence.
3. **family-a page writes.** Map page creation, activation, refresh, and
   scheduling to the four `admin_page.*` operations. Risk: a user-visible
   dashboard can publish or refresh the wrong immutable pair.
4. **family-a settings and flags.** Map settings and flag changes to typed
   operations and fold preview into `mode: preview`. Risk: a wrong field or
   flag can change live organization behavior.
5. **family-a routing and territories.** Map forwarding, destinations, and
   territory upsert to the write registry. Risk: calls or assignment rules can
   route to the wrong destination or region.
6. **family-a membership.** Map `invite_admin_organization_user` to
   `organization_membership.invite`. Risk: a wrong role or organization can
   grant access and send an email.
7. **family-b call query.** Consolidate the three call-recording reads behind
   the Core call query source. Risk: the agent could mix internal meeting data
   with caregiver voice-call data.
8. **family-b call writes.** Map tags and analysis submit/update to the write
   registry. Risk: tag replacement or stale analysis correction can alter
   downstream summaries.
9. **family-b health reads and marker.** Consolidate health metrics, rollups,
   PostHog users, and quality review behind query, and map
   `mark_account_reviewed` to a write operation. Risk: wrong scope or local
   date can produce false health conclusions or suppress follow-up.
10. **family-b Slack descopes.** Keep `post_account_health_threads_to_slack`
    and `post_daily_tool_quality_review_to_slack` deferred under d3. Risk:
    operators lose automated Slack follow-up until the later Slack bundle.
11. **family-c provider query.** Consolidate all GitHub, Linear, Notion,
    calendar, PostHog, Datadog, Logfire, Braintrust, Intercom, and Snowflake
    schema reads behind normalized query adapters. Risk: provider-specific
    authorization, freshness, and pagination behavior can be flattened.
12. **family-c codebase entry.** Consolidate the codebase intelligence names
    into one `codebase` intent union. Risk: broad routing can select synthesis
    where the user needs exact source lines.
13. **family-c external writes.** Map GitHub review, Linear workflow mutation,
    and Notion preview/execute to the write registry, but keep them unmounted
    until rung 5 and approval. Risk: each operation changes a third-party
    system or shared document.
14. **family-c incident descope.** Descope `get_current_incident_io_on_call`
    as a single-consumer incident relic. Risk: on-call ownership is less
    discoverable from the admin surface.
15. **family-c source-health descope.** Descope
    `compare_admin_agent_source_health` as a single-consumer diagnostic. Risk:
    the agent loses one specialized source-health comparison report.

### parallelization summary

The three family design and read-adapter PRs can run in parallel after the
ledger accounting and core contracts. All three depend on rung 4 for query
adapters. Mutation implementation depends on rung 5. Family-a first writes
are notes and settings. Family-b and family-c external writes remain later
because they need the write registry and explicit approval. Family-b Linear
follow-up execution must wait for the family-c operation contract or remain a
non-executing intent receipt. No family depends on another family's read
adapter.
