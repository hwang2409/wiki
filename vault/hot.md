---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-14
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **phoebe fleet EMPTY as of 2026-07-14 ~13:15Z** — PHO-13646 MERGED (#11271 `00eac05ab1`): admin agent batch account-health rollups. `get_admin_account_health_rollups` (≤50 orgs/call, two 7-day windows, ~14k calls → ~7 for 151-org cohort), Slack one-call posting up to 250 accounts, 90-min turn budget both admin paths (internal + Slack), 25-min lease fence scoped to daily run. Whale-safe day-slicing preserved (28-day recurring_shift bound, pre-SQL window-width guard, seeded equivalence proof); 50k gate + no-index constraints untouched. Linear Merged; worker archived; worktree/branch cleaned.
- **Post-#11271 manual ops for Henry**: (1) rollout is safe-by-default behind `ACCOUNT_HEALTH_BATCH_ROLLUP_ENABLED` — enable to activate batch lane; (2) watch first nightly pulse after enabling — first live exercise of batch rollup + one-thread-per-org posting at 151-org scale; (3) PHO-13647 open: PostHog users metric group descoped from #11271, still per-query.
- **Post-#11222 manual ops still pending**: delete/revoke 6 prod secrets `ADMIN_AGENT_SNOWFLAKE_{DATABASE,PAT,ROLE,SCHEMA,SQL_API_URL,WAREHOUSE}` — PAT worth revoking soonish. Also: "filled within 24h" metric REDEFINED to last-minute proxy (shift created ≤24h before start).
- **Unpushed race fix parked**: `b4d5e7f9` on `henry/staging-modal-deploy-investigation-2`, worktree `.codex/worktrees/staging-modal-2` kept — staging-deploy workflow_run race (checks out stale head_sha). PR it or drop.
- **cdx render gap RESOLVED 2026-07-14** — WIKI-99 (#80) archived-events path verified live post-relaunch: archived PHO-13559 session serves 500 structured events, zero terminal blobs. No wiki ticket needed. Wiki fleet EMPTY; bundle rebuilt 09:43 + relaunched 09:44 local, both orchestrators recycled clean. Wiki vault reconciled: 11 merged tickets pruned from todo, 9 stale worktrees removed; kept `wiki-43-terminal-fidelity` (unmerged ~500-line commit, no PR — ship or drop), `wiki-41-native-surfaces` + `wiki-24-hidden-probe` (dirty).
- **Orchestrator replaced 2026-07-14 ~09:45 local** (prior session cb6cace6 died post-merge mid-vault-logging); recovery clean, all durable steps completed.

## Recent facts

- PHO-13646 gate history: round 1 found 1 blocker + 3 majors + 4 minors, all steered; round 2 adversarial verify confirmed 7/8 + caught residual Slack-path timeout (180s turn vs 480s tool); final deltas (lease + Slack timeout) reviewed inline, clean.
- Curated-catalog cost audit method: prod EXPLAIN via readonly tunnel role ≠ tool path (admin_agent_readonly + app.mode) — 5-8x divergence; always validate through worker's real-path harness, worst-case org (e32e0940, 232k contact_attempts/28d), all 226 orgs.
- `contact_attempts` deliberately has NO (org, created_at) index — heap fetches dominate; templates split/window-capped instead. Gate stays 50k, fail-closed.
- Provider hang playbook proven: no events N min → nudge → interrupt + resend → `/replace` (same model, in-place, worktree/branch/PR survive).
- Spawn contract: ALWAYS pass real worktree as `workdir` (not repo root) — fixes Wiki.app transcript mapping.
- Modal image packaging class-bug OPEN: eagerly-imported lib reading repo files outside mounted dirs breaks voice deploys silently; no CI boots image fs. Candidate ticket: import-smoke + staging-deploy failure alert.

## Watchouts

- Schema-touching PRs: `migrate apply` locally before commit; catalog (78k) conflicts → regenerate, never hand-merge.
- Investigation-only ship-shaped findings → file Linear ticket IMMEDIATELY.
- Slack mrkdwn caps: section 3000, blocks 12000.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
