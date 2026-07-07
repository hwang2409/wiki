---
type: reference
tags: [phoebe, admin-agent, architecture]
created: 2026-07-06
updated: 2026-07-06
---

# Internal Admin Agent — Architecture & Complete State

**Read this before any Internal Admin Agent work.** One-stop context: architecture, code map, full shipped inventory (ticket/PR refs), safety model, incidents, and current state. Sources: `~/me/dox/admin-agent-log.md` (full history), `~/me/dox/admin-agent-plan.md` (queue detail), plus verified code/prod state as of 2026-07-06. Queue: [[todo]].

## What it is

Internal AI agent at `/admin/agent` (web) and Slack admin mode: investigates prod issues, queries every internal data source (Postgres, traces, Datadog/PostHog/Logfire/Braintrust, Linear/GitHub/Notion/Calendar/Granola/Snowflake/Slack/Core), spawns durable subagents, and probes the customer-facing General Agent inside seeded sandbox orgs. Internal-only, fail-closed on `User.admin`.

## Code map

| Area | Files |
|---|---|
| Runtime assembly | `libraries/python/phoebe_event_agent/runtime_assembly.py` (`build_phoebe_run_bundle` — high blast radius) |
| Tool registry + contract | `libraries/python/phoebe_event_agent/admin_tool_registry.py` (`AdminToolDefinition`, `read_only=True` enforced at construction), `admin_tool_controls.py` (audit/budget/approval lanes) |
| DB query tool | `libraries/python/phoebe_event_agent/admin_database_query.py` (SELECT-only via `admin_agent_readonly` role; connect-time privilege probe rejects write DSNs) |
| External integrations | `admin_external_api_clients.py` (typed clients, env keys incl. `ADMIN_AGENT_GITHUB_APP_ID/_INSTALLATION_ID/_PRIVATE_KEY`), `admin_external_api_tools.py` (model-facing tools), `admin_external_api_contracts.py` (input schemas, #10474) |
| Core accounts | `admin_core_api.py` → Core `/api/admin-agent/*` (Bun service: `core/src/accounts/admin_agent_read.ts`, `admin_agent_account_list.ts`) |
| Subagents | `subagent_tools.py` (worker caps: `GENERAL_SUBAGENT_ACTIVE_WORKER_CAP=6`, recommendation cap 16), `subagent_child_runs.py` (child lifecycle, fan-in, `SubAgentTerminalFinalization`), `admin_general_agent_probe*.py` (probe harness + fan-in) |
| Scheduler | `libraries/python/agent_events/scheduling.py` (`claim_next_schedulable_run(organization_id, ttl_seconds, execution_backend, candidate_run_ids)` — lease + `lease_generation` fencing), `leases.py` |
| Worker execution | `services/worker/handlers/phoebe_event_agent/process_agent_run.py` (lease heartbeat, `INTERNAL_RUN_STALLED_HEARTBEAT_FORFEIT_AFTER_S`), `run_inbox_forwarding.py` (child→parent event forwarding cron) |
| API routes | `services/api/routes/admin/agent.py` |
| Web UI | `apps/web/routes/_app/admin/agent/` (chat/graph/inspector), `apps/web/routes/_app/admin/agent-traces/` (trace tabs, judge rail, page-context sidebar), `apps/web/components/admin/` |
| Audit | `app.admin_tool_audit_events` (run_id, org, tool_name, status, budget_consumed, payload, result sizes) |
| Slack | `admin_slack_registry.py`, `admin_slack_contracts.py` |
| Evals/workbench | `evals/suites/seeded_org_callout/`, `evals/runners/seeded_org_callout_runner.py`, `evals/ui/seeded_workbench.py` |

## Architecture & runtime

- **Run/event model**: durable `phoebe_agent_runs` + typed events; pending events drain into turns; committed events feed the API stream. Typed kickoffs: `subagent_init` (PHO-12041/#9542), `page_context_init` (PHO-12111/#9677).
- **Scheduler/leases**: worker claims via lease with `lease_generation` fencing; heartbeats keep it. Hardening: probe lease-race fix (PHO-12073/#9567), activity-aware forfeit + 5-min INTERNAL turn timeout (PHO-12147/#9628), off-loop blocking reads so DeepWiki can't starve heartbeats (PHO-12826/#10288). Cancellation: terminal runs terminalize command rows; only fresh user/Slack messages revive (PHO-12857/#10328).
- **Steering**: Enter=steer / Shift+Enter=queue (PHO-12427/#9885); Slack is queue-only (PHO-12263/#9682).
- **Subagent doctrine stack** (complete): task rows (`admin_investigation_executions/tasks`) → durable hidden SUBAGENT child runs (PHO-11849/#9428) → quality contracts (PHO-11892) → follow-up routing (PHO-11916, explicit-UUID PHO-12672/#10123, natural single-target PHO-12688/#10138) → auto-delegation doctrine (PHO-11940) → cheap tiers + auto-escalation Haiku→Sonnet→Opus (PHO-11941/11981) → batch create max-12 (PHO-11942, cap bumps PHO-12209) → fan-in dedup by `terminal_event_id` (PHO-11943) → callback-first fan-in (PHO-11994, admin PHO-12585/#10023) → quiet child output (PHO-11988/12072) → bounded worker pool FIFO (PHO-12014).
- **Event-driven fan-in**: parent pauses `subagent_fan_in_pending`; durable `child_completed` events wake it (PHO-12584/#10026). Recommendation children return structured row-index payloads; fan-in maps rows→IDs server-side (see [[recommendation-subagents]]).
- **No-ceiling runtime**: turn/tool/time/fan-in ceilings removed for Admin/SUBAGENT/probe runs (PHO-12587/#10028); customer-facing runs keep bounded defaults. Safety = lifecycle cancellation + lease loss + backpressure + write gates, not caps.
- **Execution backends**: EVENT (prod workers) vs LEGACY; eval pump claims LEGACY with tree-scoped `candidate_run_ids` (#10480) so local workers don't steal eval runs.

## Tool surface (shipped)

- **DB**: `query_admin_database` SELECT-only (PHO-11219), aggregates/group_by (PHO-11507), no row caps — pagination/artifacts instead (PHO-11518 no-limits policy).
- **Traces**: `inspect_agent_run_trace` + cost attribution (PHO-11220/12018), env-sticky retries (PHO-12349), loop/retry annotations (PHO-11276), `sweep_agent_health` (PHO-11934).
- **Observability**: Datadog (PHO-11221, monitor-by-name PHO-11456), PostHog (PHO-11222), Logfire intents (PHO-11435), Braintrust (PHO-11525). Cross-env staging source from prod deploy (PHO-12162/#9687; network path PHO-12294 parked/dropped from queue).
- **Linear**: workflows + approval-gated mutations (PHO-12307), identifier/filter fixes (PHO-12939/#10474), default-project scope fix (PHO-12830).
- **GitHub**: **App-credentialed read lane DONE** — creds `ADMIN_AGENT_GITHUB_APP_*` in prod, installation-token minting, App-native repo visibility + `dynamic_visibility` readiness (PHO-12666/#10238); tools: `list_admin_github_repositories`, `search_admin_github_pull_requests`, `inspect_admin_github_pull_request` (+diff), `get_admin_github_file`. **Sub-task B MERGED (#10622, 07-06):** `POST /webhooks/github/admin-agent`, fail-closed `ADMIN_AGENT_GITHUB_WEBHOOK_SECRET` verification, normalized `admin_agent_github_webhook_events` persistence, and route tests for signature/dedupe/event-family/unknown-event handling. **Webhook activation (07-06):** secret set in prod `phoebe-app-env-vars` (`ADMIN_AGENT_GITHUB_WEBHOOK_SECRET`), App `phoebe-ci` webhook configured + 7 events subscribed via UI (hook-config API 404s until first UI setup); deliveries 401 until next prod API deploy loads the secret — redeliver from App Recent Deliveries after. **Leg C MERGED (PHO-13111/#10651, 07-06):** PR review pipeline — `review PR <url>` trigger, bounded review subagent, scoped write token (pull_requests:write only, payload-asserted tests), head-SHA discipline + push dedupe, audit row per write, repo allowlist fail-closed, live-mode guard, write-lane source health. **Remaining (PHO-12306):** D PR creation, E write gates, F UX. In-flight nearby: #10552 (woodbridge) adds repo primitives + deploy doctrine skill — blocked on missing `actions: read` App permission.
- **Notion**: MCP workflows, sharing = the boundary (PHO-12308/12540). **TRAP: `ADMIN_AGENT_NOTION_INTEGRATION_TOKEN` === legacy `NOTION_API_TOKEN` (same "Phoebe" bot) — page-connection edits hit A2P legal docs too. Dedicated integration recommended, not done.** (07-06: A2P opt-in 404s were Notion-side page-connection loss, agent exonerated.)
- **Calendar/Granola/Snowflake**: read-only Calendar MCP (PHO-12310); Granola native `GRANOLA_API_KEY` ingestion path preferred, MCP smoke-only (PHO-12539); Snowflake MCP w/ PAT (PHO-12382).
- **Slack tools**: allowlisted reads + guarded writes, bot-membership boundary (PHO-12471/12533).
- **Core accounts**: read-only Core API source (PHO-12921/#10400) + owner filter/pagination/projection/evidence-opt-in (PHO-12980/#10611, merged 07-06).
- **DeepWiki/codebase**: `inspect_codebase_wiki` → hosted bundles → `investigate_codebase` macro, `find_codebase_references`, `localize_codebase_issue`, sharded lazy artifacts (PHO-11737→12775 arc). **KNOWN BROKEN: `inspect_codebase_wiki` 58/59 prod calls fail with no error_category (PHO-13074).**
- **Ops controls**: source health, flag drift, previewed batch changes (PHO-12373), approval-gated org flag tools (PHO-12340), flag catalog (PHO-12699), `find_admin_settings_location` (PHO-12129).
- **Workflow skills**: typed skill layer over `view_skills`/`load_skill`, tool-name validation (PHO-12785/#10254). Playbooks: schema/runner/library (PHO-11261-11263).
- **Automations**: `AgentAutomation` substrate, deploy-summary archetype, no self-triggered loops (PHO-12479/#10039).
- **Sandboxes (NEW, in flight)**: PHO-13073 substrate-agnostic code-sandbox tools (create/run/read/write/export_diff/destroy/list), exe.dev scoped-token backend, TTL sweeper, audit-only lane — PR #10608. exe.dev pilot PHO-12930 (shapes in [[exe-dev]]; free tier 25GB likely too small; Modal = production pivot). Hard rules: no PHI/prod DSNs in sandboxes; only network-injected read-only repo creds; PR authority stays on App path.

## Safety & policy

- **Read-only structural**: dedicated `admin_agent_readonly` PG role + connect-time probe (PHO-11311); registry rejects non-read-only definitions at construction; writes = distinct definitions.
- **Approval lanes (PHO-12515/#9961 + PHO-12929/#10428)**: audit-only/no-approval = bounded external outputs (Linear/Notion/Slack/GitHub drafts/Calendar/sandbox fixtures). Approval-gated = Phoebe prod/customer mutations (flags, org settings, phone routing, prod DB, live outreach). Children never get approval-gated tools.
- **Untrusted data**: `<untrusted-data>` wrapping everywhere external content enters (PHO-11316/12301/12235); UUIDs stay raw for handoffs.
- **Economics**: summary-first tiering — compact model-visible output, full payloads to `admin_artifacts`, `inspect_*` drill-down (PHO-12938/#10448); cache-friendly stable prompt prefix (PHO-11689/12831). Lesson: cache hit rate, not model tier, is the cost lever; 5-min cache TTL expiries rewrote ~260k tokens each in the $9.98 run.
- **Audit**: every tool call → `admin_tool_audit_events` with scope + result sizes.

## Slack & UI surfaces

- Slack admin mode v2: triple-layer gating (platform-admin AND internal-email AND workspace allowlist) (PHO-12114/12136); thread context auto-load w/ URL resolution (PHO-12235); web↔thread reflection (PHO-12594); `:eyes:` on trace alerts → one idempotent diagnosis run in-thread (PHO-12955); safe auth-failure acks (PHO-12133). Parked: Orchard-report migration to owned threads (PHO-12773).
- `/admin/agent`: chat + execution graph (PHO-12415/12542), quieter redesign (PHO-12678), run-events inspector + bounded JSON payload viewer (PHO-12429/#10319 — reuse this for renderers), queue/steer, Agent Lab (PHO-12416), tier picker, saved investigations. `load_skill` transcript cards now preserve human-readable skill `display_name` values instead of lowercasing/humanizing them, and admin workflow skill names are Title Case (PHO-13095/#10632). In flight: type-aware output renderers (PHO-12937, PR #10614).
- `/admin/agent-traces`: raw-first tabs, judge/justice rail (PHO-12299), human review workflow (PHO-12848), Linear-style filters (PHO-12931), embedded sidebar via `page_context_init`.

## Probe system & eval loop

- Probes run the real General Agent runtime under explicit org/mode, fail-closed, hidden from product surfaces (PHO-11989/12040); routed through subagent orchestration after the bypass incident (PHO-12139-12141). Seeded orgs: `is_admin_probe_fixture`, `SmsProvider.MOCK`, `+1500` numbers, write caps (PHO-12039/12305).
- Recommendation-subagent lineage: full arc in [[recommendation-subagents]] — live-promoted (PHO-12545), then overfit exposed by cleaned fixtures (#10480); fixes in flight on #10475 (memory evidence in child rows, recall-repair deletion, safe_core deletion).
- Quality loop: golden evals w/ rubric discipline (PHO-11223/11585), Agent Court judges/justices (PHO-12689), human-sovereign trace review (PHO-12848), tool telemetry dashboard (PHO-12951), `report_missing_tool` demand capture (PHO-12952), expected-vs-alertable failure monitors 288625692 / 302175306 (PHO-12949/12962).
- **Harness doctrine (PHO-12982)**: (1) manage economics the model can't perceive; (2) tools meet the model's priors; (3) safety structural never behavioral; (4) failure legible and stored; (5) experience compounds into structure; (6) close the loop with reality; (7) thin where the model is strong, thick where it's blind — test: "when the next model is 2x better, does this piece gain or lose value?"

## Incidents & lessons (root causes)

- Compaction wedge 06-19 (`019ee19f`): prompt/schema mismatch → retry storm → 3 pods wedged (fixes PHO-12147/12151/12152).
- Polling storm 06-18: 1.5s UI polling exhausted shared DB pool → 234 customer 429s → SSE snapshot (PHO-11983).
- Probe fanout bypassed doctrine stack → PHO-12139-12141.
- DeepWiki heartbeat starvation (PHO-12826); cancelled-run revival via leftover command rows (PHO-12857).
- $9.98 run (`019f243e`): cache expiry + raw output + Linear retry loop → tiering (12938) + tool specs (12939).
- Linear "permission gap" 07-02: client `id:{eq:<non-UUID>}` filter bug, NOT credentials; error bodies swallowed (12939/#10474).
- Migration lock-queue prod blip 07-06: ADD COLUMN on app.organizations queued all traffic (see [[migration-lock-queue-outage|phoebe/til/migration-lock-queue-outage]]).
- Worker Fargate startup CPU saturation (PHO-13075, unassigned): admin agent was victim (pending_event.age ~2h), not cause.

## Chronology

06-09/12 foundations (shell/registry/read-only/audit/sources) → 06-11/12 idea wave (texQL/data-products/trace-UX, mostly parked) → 06-15/19 doctrine buildout (subagents, DeepWiki, probes, skills) → 06-19/25 surfaces + MCP integration wave + approval simplification → 06-26/30 event-driven fan-in, no-ceiling runtime, live recommendation promotion, API-client migration start → 07-01/03 hardening + quality loop → 07-05/06 fixture-leak cleanup + overfit exposure, sandbox tool surface, accounts API, webhook ingestion.

## Planned: package + database split (PHO-13093, P0)

Decision 2026-07-06 rev2 (see [[admin-agent-split|phoebe/decisions/admin-agent-split]]): **Phase 1 = pure extraction** — all admin code moves to `libraries/python/phoebe_admin_agent/`; General Agent plumbing + shared mechanics stay in `phoebe_event_agent`, imported via barrel. Phase 2 = dedicated admin RDS (INTERNAL/SUBAGENT runs + `admin_*` tables; probe runs stay in app DB; ID+forwarding links, no cross-DB FKs; dedicated admin worker service). Deferred: `agent_runtime` mechanics extraction, per-agent policy forks. Sequencing: after #10608 merges (#10475 overlaps one probe file, rebase-manageable). NOT a clean git mv: shared files (runtime_assembly, subagent_tools/child_runs, translators, Slack routing) need function-level carve-outs; shims must be lazy deep-path modules to avoid barrel import cycles — full entanglement inventory is the 2026-07-06 comment on the ticket. Code map above is PRE-split — update when PHO-13093 lands.

## Active right now (2026-07-06)

- PHO-13073 sandbox tools — PR #10608, review findings fixed, Bugbot flake + human approval pending
- PHO-12937 output renderers — PR #10614, CI babysit
- PHO-12306 leg B webhook ingestion — PR #10622 open; CI/review babysit
- PHO-12634 API-client port — PR #10652 merge-ready: removes stale `ADMIN_AGENT_MCP_*` production fallbacks/readiness/docs; CI green + review handled, awaiting human approval
- PHO-12930 exe.dev pilot — blocked on 13073 tools + plan-upgrade decision
- #10552 (woodbridge) — reviewed 07-06: blocking on missing `actions: read` App permission
- #10475 recommendation-subagent fixes — separate session; full bank rerun owed on cleaned fixtures

## Parked / open threads (not in queue)

texQL (PHO-11256-11260), data products (PHO-11269-11272), trace explorer/compare/diff (PHO-11273-11275), memory/artifact actions (PHO-11251/11264/11267/11268), agent-as-MCP-server (PHO-11539), daily run review (PHO-11540), Snowflake native (PHO-11598), Voice QA (PHO-12134), record-and-inject (PHO-12211), multi-shift outreach execution (PHO-12424-12426), Orchard Slack migration (PHO-12773), post-run reflection supply side (12982 §5), orphan QUEUED-task TTL reconciler, `<tool_result>` XML-escape inconsistency in runtime_assembly, write-capability risk ladder (designed in log Reference section, realized piecemeal).
