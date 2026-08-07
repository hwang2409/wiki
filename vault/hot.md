---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: **Rendering arc round 2 COMPLETE 2026-08-07 — WIKI-263 #199 + WIKI-264 #200 merged (both cdx luna + sol review loops).** #199: codex exec telemetry preamble (`Script completed/terminated/failed`, `Wall time`, `Output:`) stripped to metadata, outer-envelope-only, status precedence structured>preamble>body-inference — unlocks WIKI-261 numbered-read highlighting on codex runs. #200: user-installed fonts served via sidecar (`/api/fonts` + fd-walk file endpoint) — WKWebView can't resolve `~/Library/Fonts` (fingerprinting protection), so since WIKI-256 the app silently fell back to SF Mono; now FontFace-registered webfonts, lazy-loaded, variable faces + compound style weights handled. 4 review rounds; TOCTOU closed STRUCTURALLY (openat component walk from trusted `/` fd incl. root components, no serve-time path resolution — good pattern for any future file-serving endpoint). **Henry: REBUILD + relaunch Wiki.app — picks up #199/#200 plus yesterday's five.** Prior lessons hold: watch-tool false-fire → poll PR head SHA; worker lifecycle doctrine in protocol doc. Parked: store-pruning P2 (next pick), composer/command menu P2, worktree-debris P3 (~100 stale), artifact-rendering arc, WIKI-233 reds triage, WIKI-228/229, P2/P3 UI ideas.
- **PHOEBE (orch `phoebe-dev`) — v3 ladder arc: FOUNDATION LANDED 2026-08-06 ~21:00 ET:**
    - **#13505 MERGED to main as squash 511c1696** (Henry's merge word; approval + 8 review rounds + Bazel CI + buildkite gate green at 2d63432d).
    - **CI-outage saga resolved**: GH Actions dropped events all evening; CI moved to Buildkite (`CI_RUNNER=buildkite` 20:16Z, gate REQUIRED on main alongside Bazel CI which reports via remote-bazel commit status, NOT Actions). Fix that unstuck the ladder: copy main's `.buildkite/` verbatim onto stacked branches (Henry's idea). Gate scripts were patched twice on main same evening (#13605 token fix, #13607 main-gate) — keep stacked branches synced with main's exact `.buildkite/` or gate insta-fails in ~26s.
    - **Post-squash conflict sweep COMPLETE 2026-08-06 late eve**: all four open PRs conflict-free with feature-only diffs vs main (worker-per-PR, per-file resolution rules, goldens regenerated). #12574 closed per Henry.
    - **#13557 (to_sandbox opt-in): CLEAN + APPROVED + 6/6 checks green at f5f35515 — awaiting Henry merge word only.**
    - **#13606 (PHO-15330 start_monitor)**: review-clean (r1 8 findings + r2 pytest-mock deps fixed, Devin threads resolved); post-merge Bazel CI red root-caused by CI-fix worker, fix pushed at 11f26772, CI cycling; retarget reset review to REVIEW_REQUIRED — needs Henry re-approval + merge word. Design: one-shot polling tool `start_monitor(...)`, poke wakes, lease-fenced, due-monitor sweep.
    - **TUI DIRECTION CHANGE (Henry 2026-08-06 eve): don't rebuild opencode — put v3 agent BEHIND the stock OpenCode TUI.** Research verdict (vault-worthy): `opencode attach <url>` is a supported path; build a python shim implementing opencode's OpenAPI/SSE server contract (sessions, Part[] messages, prompt/abort, permission.reply, /event deltas), stub the rest, pin the opencode release. Risks: protocol churn, bootstrap coupling. Fallback: fork packages/tui (MIT, TS). Shim prototype worker: awaiting Henry go. #13587 (custom TUI, conflict-free draft at 84d88aa3) stays as fallback. /tmp/v3-tui-test worktree = Henry's manual test env (clean up when done).
    - #13538 control surface: conflict-free draft at 54af4c60.
    - Closed today: #13413+#13416 (PHO-15253 canceled), #13414 (PHO-15254 canceled), #13540, #13558 write door (Henry, no comment — disposition parked). Linear writes: OAuth'd MCP live this session; agent-token mint blocked on expired AWS SSO.
    - Watchers: gh_pr_watch monitors on #13557 + #13606 (13505's stopped post-merge); worker status monitors. Wrapper: /tmp/agent-status/pr_watch_summary.sh.
    - Devin-rule skill/AGENTS edits still UNCOMMITTED in main checkout (+ revenue_accounts edits, Henry's, don't touch).
    - Rungs left: control #13538 (needs retarget/rebase onto main), #13193 parity harness rebase. #13408 closes when covered.
    - Streaming-monitor follow-up parked: Devaj researching cc-style stream monitors; upgrade path = keep start_monitor wake/durability, swap detection layer; arbitrary-code question is the design decision.

## Watchouts

- **Fleet routing: implement = cdx gpt-5.6-luna, review = cdx gpt-5.6-sol, orchestrator = cc fable-5; frontend-design = cc opus-4.7.**
- Post-squash stacked-PR merges: conflicts are semantic (feature vs squash) — worker with per-file resolution rules, never blind ours/theirs; goldens regenerate from merged code.
- NO LOCAL BAZEL for workers; bb remote evidence with pasted rev-parse; PR CI authoritative.
- Pre-push: `prek run --hook-stage pre-push` once, --no-verify documented for hook-infra failures.
- Review-loop: verdict → archive reviewer → compact contract → replace implementer → steer now; reviewers read-only.
- cdx workers hallucinate SHAs — verify head MOVED via gh before acting on reports.
- Henry decisions parked: PHO-15255, PHO-14037, PHO-15223, #13384 migration, #13417/#13558 disposition, #13587 + #13538 retarget.
