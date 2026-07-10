---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-10
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **phoebe admin-agent arc — MAJOR MILESTONE 2026-07-09/10** — 8 PRs merged into main + prod through this orchestrator session, all admin-agent surface: [PHO-13157](https://linear.app/phoebework/issue/PHO-13157) delayed survey capture (#10751), [PHO-13273](https://linear.app/phoebework/issue/PHO-13273) generated schema catalog replaces `_DOMAIN_SPECS` (#10920 — 78k JSON checked in, `search_schema`/`describe_table` discovery tools, EXPLAIN cost gate, deny-tier secret classification, `admin_agent_readonly` role with NOBYPASSRLS + 10s statement_timeout), [PHO-13277](https://linear.app/phoebework/issue/PHO-13277) admin charts as agent messages (#10930), [PHO-13278](https://linear.app/phoebework/issue/PHO-13278) unified truncation policy + artifact registry (#10933), follow-ups [PHO-13304](https://linear.app/phoebework/issue/PHO-13304) (#10961), [PHO-13307](https://linear.app/phoebework/issue/PHO-13307) (#10965), plus HOTFIX-CATALOG (#10976) + HOTFIX-CATALOG-2 (#10978) regens after schema-touching PRs landed stale catalog. woodbridge PR #10983 (call analysis approval + failure alerts) merged 07-10 with our added regression test + doc fixes. Linear all Done.
- **Active fleet (07-10)**: **cdx:PHO-13274** @23 (account-health as per-org Phoebe Slack threads) mid-refactor to call-feed shape per Henry steer — hardcode channel constant, drop feature flag, traffic-light emoji + org name parent, `slack_markdown` block detail thread reply (mirror `call_analysis_run.py:95-427` pattern). **cdx:PR-10475** @22 (subagent recommendation parity) into iteration 10+ of the Step 3↔4 loop; step-1-postfix showed verdict 0.778→0.889 + forbidden picks 10→1 but overlap regressed slightly and 2 ON-worse cases + 1 forbidden-pick pair remain. Henry directive: keep looping until parity or he says otherwise.
- **Merge-conflict pain point confirmed 07-09** — 78k `admin_readable_schema_catalog.json` regenerates on every schema change. Two hotfix cycles in 90 minutes proved it. Systemic fix worth a follow-up ticket: pre-merge CI gate that runs `bazel run //database:update_schema --diff` and blocks merge if working tree differs. Ergonomic concern flagged in orchestrator review of PHO-13273 pre-merge.
- **Wiki resolver bugs handed to wiki-dev** — 3 bugs bundled: [[WIKI-45]] cc-path resolver session_id-first, [[WIKI-46]] `_session_paths` cache invalidation on handoff, and slug-glob-cwd-not-worktree case (subsumed by WIKI-45). All in vault todo. Repro'd during PR-10475 cc→cdx handoff + VA-WHEATRIDGE cwd=main-checkout tasks.

## Recent facts

- Admin agent readonly role: `LOGIN` only, `NOBYPASSRLS`, `default_transaction_read_only=on`, `statement_timeout='10s'`, `SELECT` on `app.*` only. Runtime tripwire `verify_read_only_postgres_connection` re-checks `is_superuser=false`, `bypasses_rls=false`, `has_table_privilege('app.organizations','INSERT')=false` on each pooled connection. 19 columns denied in catalog (all real secrets — OAuth tokens, EHR API keys, encrypted credentials, push tokens). Zero suspicious readable secret-pattern columns.
- Post-truncation admin agent contract (PHO-13278): every admin tool output goes through central `admin_tool_result_caps` pipeline — trim collections → trim source links → envelope compaction → cap check → raise. Truncation marker = `{"kind","path","dropped","artifact_ref"}` uniform across all tools. Agent uses `admin_python_snippet_workflow` skill's 5 verbs (`read_run_data`/`search_run_data`/`slice_json`/`diff_run_data`/`run_admin_python_snippet`) against artifact_ref instead of re-running expensive query.
- Call feed shape reference (`services/worker/handlers/system/call_analysis_run.py:95-427`) = the pattern PHO-13274 should mirror: hardcoded channel constant, parent = scannable header (emoji + name + one-line context), thread reply = full detail via `slack_markdown(text, max_chars=11500)` block (12k Slack `mrkdwn` cap), non-fatal try/except failure. Same `post_admin_operational_slack_thread` primitive for binding to a Phoebe run.
- Adversarial diff review before merge-ready is the standard gate — delegated via code-review agent for large diffs, inline for small. Latent-vs-real MEDIUM triage: if it can trigger with real data or LLM retry (idempotency, char caps), fix inline; if purely defensive, follow-up ticket.

## Watchouts

- Wiki NATIVE app = FROZEN build: backend/frontend fixes reach Wiki.app only after `make native-build` + relaunch.
- Every schema-touching PR must run `migrate apply` locally before commit or main goes red on `//database:update_schema_0_test`. Two hotfix cycles already this week.
- `admin_readable_schema_catalog.json` merge conflicts: deterministic generator (sort_keys=True), rebase regens cleanly. Reviewer fatigue on 78k-line diffs is real.
- LLM tool retry after partial failure = duplicate Slack posts unless idempotency key persisted (PHO-13274 MEDIUM 2). Prompt-only "don't retry" guardrails are fragile.
- Slack `mrkdwn` section text cap 3000 chars (blocks: 12,000). Test parent+detail sizing against realistic org sizes.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
