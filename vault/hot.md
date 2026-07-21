---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-21
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **TOOLING MONOREPO LIVE 2026-07-20**: all 4 misc subprojects (`mitm-inspector/`, `pufferclone/`, `tix/`, `gauge/`) migrated to `github.com/hwang2409/tooling` (private) via subtree, per-project history preserved. Local at `~/me/fun/tooling`. All mitm work now PR-based against origin/main; worker worktrees under `.worktrees/<ticket>`.
- **MITMWEB CURRENT (tooling orch)**: B4+F7 merged 2026-07-20 (PRs #2, #3 — flow-summary enrichment, durable body search, conversation view). S1 durable sqlite merged. **F8 session-first UX MERGED 2026-07-21** (PR #5 squash `cd1430b`): session-list home (metadata-only rows), canonical main-thread chat drill-in (`canonical.ts` prefix chains, incremental==fresh pinned 14k randomized rounds), manual source picker w/ raw fallback, sanitized markdown (inert images). 12 sol rounds + orchestrator-fixed rounds 12-13 under dual credit exhaustion (in-session review fallback + 300k differential fuzz). **F9 readability polish IN FLIGHT** (cc/fable-5 + /frontend-design + /make-interfaces-feel-better; Henry: v0 "super dense, unreadable"; presentation-only). Full arc [[mitmweb-rebuild]]. Remaining after F9: P1 packaging; Makefile still uncommitted awaiting sign-off.
- **CODEX ALIVE 2026-07-21** (Henry-confirmed; earlier "pool exhausted until Jul 24" assumption void). Default pipeline: luna implement / sol review. After any future re-auth, replace live cdx workers so processes hold fresh tokens.
- **FABLE-5 WORKING AGAIN 2026-07-21** (orchestrators + F9 run on it; watch for credit blocks recurring — resets seen Jul 22 11am ET Claude / Jul 24 11:24pm codex). Frontend rule: frontend/UI tickets get cc fable-5 + mandatory /frontend-design (+ /make-interfaces-feel-better for polish tickets) — codified in [[orchestrator-worker-protocol]].
- **Misc clone arc** (2026-07-16): pufferclone v1+v2 (PUF-8..12, main@6bc5d64), `tix` (TIX-1), `gauge` TSDB v0 (GAU-1..5, master@9d32f0b). Next clone candidates in [[local-cloud]]: Temporal-lite vs LSM KV — Henry pending. Unticketed: cargo install tix/puf/gauge, tix vault migration, PUF-13 NVMe tier.
- **Wiki 2026-07-17 double merge**: WIKI-132 (ticket/PR dashboard, #105@6c52a60 squash, 5 rounds) + WIKI-133 (j/k scroll, #104@643628b squash, 3 rounds). 132 had rebase-on-main mid-loop after 133 landed. Dashboard = `/api/dashboard/tickets` merging supervisor registry + status files + archives + phoebe-allowlist gh-PR-cache w/ prod-deploy compare; 15s poll; component-integration tests via @testing-library/react (dev dep added). Sidecar rebuild debt: #103 backend changes not in deployed sidecar; NOW also #105 backend + frontend not deployed — fold into next swap (staged flow: FORCE_STAGE_ONLY=1, SIGTERM supervisor, swap-native-app.sh, relaunch). Open: daemon-ize backend, WIKI-126 rebrand blocked on name, compact-mermaid-preview unfiled, WIKI-92/109/94 flake candidates.
- **Phoebe** (phoebe orch, RCA-driven ops 07-18/19 — 5 merges): 07-19: PHO-14060 exception-noise cleanup MERGED (#11763 squash 236463b2, 1 clean sol round — wellsky api_host_forbidden + rollback-observability skips no longer log exc_info, kills ~2.5k fake exceptions/day). PHO-14061 agent-correctable admin tool validation errors MERGED (#11764 squash b9371707, 4 sol rounds — boundary ValidationError->corrective field messages, provider schema honesty for Any/null, fields-set presence). 07-18: PHO-14053 wellsky clock-out (#11747 0fae1222, 4 rounds), PHO-14047 DRI auto-join (#11745 946ba0c9), PHO-14046 retry observability (#11744 a72d5435). RECURRING: logfire 30-min sweep loop ARMED (session cron e720f044, 11,41 * * * *, auto-RCA + spawn-if-urgent, known-noise baseline in prompt). OPEN GATES (Henry): PROD DEPLOY GAP — last deploy 07-18 19:09Z @ 946ba0c9; #11747/#11763/#11764 + others undeployed, deploys manual/scheduled not merge-triggered. Avondale HHAX -9 GetPatientDeclinedCaregivers CS escalation (client sync dead since 07-14; optional code fix unticketed). Green Tree voicemail-transfer = CS config (ivr+post_dial_digits recipe, org on nonFixedVoip/Sinch; no code). Sign-in circular-JSON RCA parked (posthog empty, sentry CLI unauthed). PHO-14023 gallery pick, PHO-14029 prod dedupe apply, checkout disposition all still pending. rca skill at .claude/skills/rca/SKILL.md (uncommitted, works well). Fleet doctrine addition: fold adjacent cases into open PR (no dud PRs); quiet parked merge-ready re-alarms by clearing watchlist row.
- mitm-inspector requires Python 3.12 — sequencer intentionally rejects 3.13; `uv sync --python 3.12` in every new worktree (uv defaults to 3.13).
- mitm merge flow: local repo, no remote/PR — merge --no-ff into main, gates on merged main, remove worktrees, delete branch.
- Reviewer state field unreliable — verdict step text authoritative, always.
- Steer mode default `now` (Henry); on-idle only for genuinely-next-task input.
- Fleet monitor doctrine (Henry): watchlist file + re-alarms (verdict q5m, review-gap q10m, staleness 30m); never rebuild monitors per spawn/archive.
- `wiki` CLI not on monitor-shell PATH — use `~/me/fun/wiki/wiki` absolute.
- Supervisor MCP ops can 1s-timeout under load — verify effect before retrying steers. Soft cap 5 workers; above it expect provider timeouts.
- Workers survive backend restarts; status file `/tmp/agent-status/<T>.json` readable when backend down.
- cdx-only gotchas (dormant until quota reset): plugin-install elicitation hangs turns — ban in spawn prompts; cyberPolicy kills "adversarial/attack"-phrased review prompts — phrase "correctness/robustness"; startup hangs — replace_agent; after token re-auth replace every live cdx worker.
- Phoebe: BuildBuddy ExecuteWorkflow reruns check out raw branch (no main merge) — for fixes-on-main, merge origin/main + push. Bazel cache can hide time-bomb tests — unchanged-target red → suspect main. Worker test policy (Henry): no full local suites, directly-touched targets only, CI authoritative.

## Watchouts

- NEVER paste exploit-style payload strings (script URLs, XSS probes) into agent status text, steers, or chat — Anthropic AUP filter kills the session unrecoverably (poisoned context; replace_agent only fix). Payloads live in test files only. Bit MITMWEB-F8 2026-07-21.
- Wiki backend flapped repeatedly 2026-07-21 ~10:30-10:50 ET (app swap): supervisor restart mass-archives workers as handoff and respawns orchestrators — check `list_agents` after any restart, respawn lost workers with takeover kickoffs (uncommitted worktree edits survive). Vault rolled back once in the same window — verify vault notes against git/GitHub reality after restarts.
- Next free ticket ID: WIKI-134 (131 = chat scroll e5370de, 132 = dashboard #105@6c52a60, 133 = j/k scroll #104@643628b).
- Mastermind merge-ready loop cap raised 3→8 (see [[feedback_merge_ready_iteration_cap]]) — gpt-5.6-luna needs more rounds; auto-steer unless plateau on same finding for 3 straight rounds.
- Pin review worktrees + gates to exact SHA; archive reviewer immediately after verdict routed.
- Workers' claims verified by mutation — keep ordering it in review prompts.
- Local main can hold unpushed vault commits — pull --rebase before push.
- `.codex/worktrees/` ~40 stale entries — prune pending Henry.
- CI polling: use `gh_pr_watch.py --watch` (babysit-pr script), NEVER bash `until [ "$(gh pr checks ... | awk ...)" = "pass," ]` — awk/sort predicate silently misses pending→green (bit us on PHO-14003 #11688 2026-07-17).
- Steer cap: `steer_agent.text` max_length = 4000 chars — long multi-finding steers hit ValidationError. Write full detail to `/tmp/<ticket>-review<n>-findings.md`, send terse steer pointing at file path (bit us on MITMWEB-F5 REVIEW1 2026-07-17).
