---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-14
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **Second arc 2026-07-14 evening: THREE PRs shipped, fleet EMPTY** — WIKI-108 (#87, 47cddb0) sidebar pages open as separate windows (utility:// panes, reuse-and-focus); WIKI-109/110 folded (#88, 58c6c30) Cmd+K session search + C-a p blank-pane placement (move-only-from-blank, pane-state keys retained); WIKI-111 (#89, 14e78d2) **built-in agent harness**: WIKI_RUNTIME_CARD v1 injected into every cc/cdx run (role-flavored: orchestrators get spawn/steer/archive call shapes, workers get no-fleet-authority + status contract), `wiki agent spawn/status/steer/replace/archive` CLI verbs, backend URL autodiscovery (`~/.wiki` published loopback file; fixes watch dead-port), orchestrator MCP fleet ops (role-enforced at tools/list AND tools/call), subsumes WIKI-88. Gates: 1 iteration each, 0 BLOCKING/HIGH across all three (contrast: morning arc caught 8 BLOCKING).
- **Wiki.app rebuilt + relaunched with #87/#88/#89 — harness E2E VERIFIED** — runtime card injected (orch + worker), MCP `spawn_agent`→cdx worker (TEST-1, gpt-5.6-terra) rendered sidebar mermaid artifact `6d0e7d00` from cold spawn, `steer_agent` mode=now delivered + ACKed, `archive_agent` clean (run a2a4ff74, outcome closed). Full spawn→work→steer→archive loop green, no tmux. Untested still: `wiki agent watch` autodiscovery.
- **Earlier today (morning arc)**: WIKI-100..105 (#81-#86) shipped + relaunched 14:01; knowledge.db rebuilt post-diet + VACUUMed 2.0G→1.0G (chunks 459M/events 277M/FTS 111M); `wiki search` live — **agents: prefer it over map.md-walk**.
- **Open tickets from today's gates**: WIKI-112 (dead-archive.playwright.mjs fails on untouched main — WIKI-7801 Archive button timeout, blocks npm-test full-chain green, reproduced 3x independently), WIKI-113 (runtime_card sync git subprocess blocks supervisor loop ≤5s + bundled #89 LOWs), WIKI-106 (rebuild VACUUM), WIKI-107 (supervisor lost-race traceback noise).
- **Phoebe (2026-07-14 evening): FIVE PRs merged, fleet EMPTY** — PHO-13676 (#11310 dedup black hole), PHO-13690 (#11325 atomic CSV export tools), PR-11284 (callout timing, merged by austinjiann), PHO-13685 (#11336 lifecycle_state_series), PHO-13734 (#11339 account note → Slack admin-agent thread, ticket backfilled Done). All gated pre-merge, zero blocking. All in prod except #11339 (merged 1min after 20:44 deploy cut — next deploy).
- **Phoebe Linear sweep DONE (re-auth completed 22:00)**: 13676/13690 auto-closed; 13685 hand-bumped Merged→Done. New tickets from run-019f628f audit ($5.45 churn-CSV run, CSV verified correct vs prod): **PHO-13733** (High: document entire admin tool surface — filter semantics/caveats/cross-refs; run burned ~$2.50 on eligible-filter trap + discovery), **PHO-13735** (High: lifecycle_state_series churn-blind — customer_signed_kickoff_date NULL on 22/29 churned orgs, backfill via revenue_accounts.close_date or anchor fallback), **PHO-13736** (regex word-boundary "demolition"), **PHO-13737** (Low, shaping: exe.dev sandbox workspace, data-governance gate explicit).
- **Phoebe carry-overs**: PHO-13646 rollup flag enabled — watch first nightly pulse; revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets; parked race fix `b4d5e7f9` — PR or drop.

## Recent facts

- Steer/message endpoint body field is `text` not `message` (MessageIn, main.py:1918) — 422 otherwise. Post-#89 use `wiki agent steer` instead.
- Sidecar auto-rebuilds knowledge.db on schema mismatch at startup — never run `wiki index rebuild` right after relaunch (lock race, WIKI-106); check `~/.wiki/knowledge.db.rebuilding` first.
- Soft cap 5 counts LIVE workers fleet-wide; warning-only. 7 ran fine this arc post-#85, but spawn under bazel load still the killer.
- Sibling frontend PRs both touching App.tsx/pane.tsx: overlap notes worked, #88 rebased over #87 with zero invariant loss — pattern proven again.
- `wiki agent watch` pre-#89 needs WIKI_BACKEND_PORT=8213; post-relaunch autodiscovers.
- Stale worktrees pending Henry: `wiki-43-terminal-fidelity` (unmerged ~500-line commit, no PR — ship or drop), `wiki-41-native-surfaces` + `wiki-24-hidden-probe` (dirty).
- exe.dev BACK IN USE: #11104 preview.sh branch previews (team plan active; pilot-canceled note was stale, tools/exe-dev.md updated). Bun exists at ~/.bun/bin but NOT on headless-shell PATH — pre-push core-typecheck fails without `export PATH="$HOME/.bun/bin:$PATH"`.

## Watchouts

- Ship-shaped findings → ticket IMMEDIATELY.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Gate independently reruns suites; demand raw result lines in PR body (no false-greens this arc, but doctrine stands).
