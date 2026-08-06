---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: WIKI-244 MERGED (#178, 2026-08-06). 2026-08-05 hotfixes landed direct to main (3c180a3 orphan-sweep once-per-boot, a0b4f0c agent-prose monospace, c1c86e2 build-script venv python). NEW spawn wave 2026-08-06: cdx luna implementers on **WIKI-242** (agent-surface a11y final pass, worktree wiki-242-a11y) + **WIKI-227** (incremental costs scan + slim /api/agents, worktree wiki-227-agents-perf); fleet monitor armed off `.wiki-dev-watchlist`; boot-sweep follow-up (terminal-run skip + store pruning) filed as P2 todo.
- **PHOEBE (orch `phoebe-dev`) — v3 ladder arc:**
    - **Merge auth changed (Henry 2026-08-06): ALL v3 ladder PRs need approval from another person on the v3 agent project — Henry cannot merge alone. No orchestrator merges.**
    - **PR 1 FOUNDATION #13505 merge-ready at c056a2dc** (6 rounds; scope-down done, containment 9/9). Blocked only on `REVIEW_REQUIRED` + merge word.
    - **NEW overnight 2026-08-06 (both review-clean, undrafted, stacked on foundation, auto-retarget to main at #13505 merge):**
        - **#13557 agent-chosen sandbox staging** (`to_sandbox` opt-in on spooling seam, threshold spill stays safety net) — 2 sol rounds, final head bc703986. r1 blocker fixed: spool wrapper idempotent under load_skill re-wrap.
        - **#13558 write door (scoped)** (`tools/write`: entity+operation registry, direct/diff-first/human-approval, diff-first = workspace artifact + locked confirm, NO tables/migrations) — 2 sol rounds, final head 5f038f15. r1 blockers fixed: org+mode association scoping, SELECT FOR UPDATE confirm, assert_never dispatch, idempotency digest conflict.
    - Henry ruling 2026-08-06: main already has the read tool → reads rung #13540 (draft) likely redundant — NOT closed, Henry's call. #13417 (207-file old writes bundle) superseded by #13558 once landed — also left open for Henry.
    - Rungs after foundation: control #13538, bash door #13416 (stacked, gates-complete), #13413 routing flags parked LAST. #13408 closes when ladder covers it. #13193 parity harness owes rebase post-carve.
    - Orchestrator watchers live: gh_pr_watch on #13557 + #13558 (CI + bot threads); fleet watchlist empty, all workers archived.

## Watchouts

- NO LOCAL BAZEL for workers: bb remote evidence MUST use `bin/bb remote --run_from_commit=<full pushed SHA> test ...` one captured session with `git rev-parse HEAD && git status --porcelain`; runner log must show FETCH of that commit.
- Worker pre-push hooks: don't let workers repair remote-bazel hook plumbing for hours — ruling: `prek run --hook-stage pre-push` once, fix real diagnostics in own diff, `--no-verify` + documented bypass for hook-infra failures, PR CI authoritative (applied 2026-08-06 PHO-0-V3-WRITES).
- Review-loop protocol: verdict → archive reviewer → compact contract → replace implementer → steer now; reviewers read-only, never run tests.
- Idle-by-design merge-ready workers trip the 1800s workgraph stall alarm → archive them once orchestrator owns the PR watch (terminal archive edge stops alarms).
- cdx workers hallucinate SHAs/completions — verify via gh that PR head MOVED + files in scope before spawning review; require pasted `git rev-parse HEAD`.
- Security-bot fail-closes on >300KB diffs → Henry --admin; regenerate goldens from merged code, never text-merge.
- Henry decisions parked: PHO-15255 seam priority; PHO-14037; PHO-15223 retry-storm P2; #13384 migration (after next prod deploy); #13540 + #13417 disposition.
