---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: Rendering arc COMPLETE 2026-08-07 early — five merges: WIKI-258..261 (#194–#197, gate-only, opus-4.7) + **WIKI-262 #198 codex transcript parity** (cdx luna, 3 sol review rounds: JS harness `tools.*` unwrap via strict lexical scanner, apply_patch decode, wait/mcpToolCall classification, outputs through claude collapse/peek/highlight pipeline; full measured-surface fixtures). Survived mid-round codex stream disconnect via resume-steer of uncommitted work. **Henry: rebuild + relaunch Wiki.app — all five fixes need it, incl. sidecar (/api/fonts + transcripts.py).** Watch-tool edge: `wiki agent watch --until merge-ready` false-fires on stale status mid-loop — poll PR head SHA instead. New protocol section: worker lifecycle doctrine (one-shot contract workers, fresh-worker-per-round, steering escape hatch). Parked: store-pruning P2, worktree-debris P3 (~100 stale), artifact-rendering arc, WIKI-233 reds triage, WIKI-228/229, P2/P3 UI ideas.
- **PHOEBE (orch `phoebe-dev`) — v3 ladder arc: FOUNDATION LANDED 2026-08-06 ~21:00 ET:**
    - **#13505 MERGED to main as squash 511c1696** (Henry's merge word; approval + 8 review rounds + Bazel CI + buildkite gate green at 2d63432d).
    - **CI-outage saga resolved**: GH Actions dropped events all evening; CI moved to Buildkite (`CI_RUNNER=buildkite` 20:16Z, gate REQUIRED on main alongside Bazel CI which reports via remote-bazel commit status, NOT Actions). Fix that unstuck the ladder: copy main's `.buildkite/` verbatim onto stacked branches (Henry's idea). Gate scripts were patched twice on main same evening (#13605 token fix, #13607 main-gate) — keep stacked branches synced with main's exact `.buildkite/` or gate insta-fails in ~26s.
    - **Post-squash cleanup IN FLIGHT: two cdx-luna merge workers live** folding origin/main into the retargeted stacked branches: `PHO-0-V3-OPTIN-MAIN-MERGE` (#13557, worktree v3-sandbox-optin; known conflicts: bash.md keep-ours to_sandbox doc, middleware quartet keep feature deltas, bash.golden.md REGENERATE never text-merge) and `PHO-15330-MAIN-MERGE` (#13606, worktree pho-15330-sandbox-monitoring; regenerate schema/models/catalog via `migrate apply`). Final diff-vs-main must be feature-only.
    - **#13557 (to_sandbox opt-in)**: retarget reset review to REVIEW_REQUIRED — needs re-approval after main merge. Prior state: approved + port-reviewed clean.
    - **#13606 (PHO-15330 start_monitor)**: review-clean (r1 8 findings fixed+verified, r2 pytest-mock deps fixed, Devin threads resolved). Merge-order gate (after #13505) now SATISFIED; merge = Henry-only. Design: one-shot polling tool `start_monitor(tool_name, params, wake_states, polling_interval_s, timeout_s)`, poke-machinery wakes, lease-fenced, due-monitor sweep. #13598 (v2 design) closed/superseded.
    - #13587 TUI polish: draft on dead foundation branch — needs retarget/rebase decision. Henry has a TUI test worktree at /tmp/v3-tui-test (clean up when done).
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
