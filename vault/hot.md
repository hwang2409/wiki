---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: **OpenCode UI arc FULLY COMPLETE 2026-08-06 — EIGHT merges in one day**: #179 a11y, #180 costs/atomic-publication/slim-API, #181 chrome, #182 two-tier transcript, #183 per-event units + quiet composer (owner call), #184 gruvbox-dark-hard-contrast palette, #185 transcript follow-ups (spacing model, live-state anchor, data-level caps), #186 **1:1 fidelity + polish wave** (gate waived; fable-5; 10-item gap table: turn meta rows, syntax highlighting everywhere, json/python pretty rendering, "polished" toggle beside raw, fence-tag honoring, embedded-heredoc structuring, file-slice output inference, cursor=text). Doctrine: [[opencode-design-direction]] (bar: "almost exactly 1:1", reference asset in vault), [[opencode-tui-design-notes]], [[opencode-transcript-doctrine]]. NEW convention in protocol doc: agents declare render types via tagged markdown fences. Fleet EMPTY, watchlist empty, monitor armed. Suites at close: 347 vitest + transcript playwright stages green. Parked follow-ups: store-pruning P2 (costs checkpoint deferral), worktree-debris P3 (~95 stale), artifact-rendering arc (mermaid/plots), WIKI-233 reds triage (likely stale), WIKI-228/229.
- **PHOEBE (orch `phoebe-dev`) — v3 ladder arc:**
    - **Merge auth changed (Henry 2026-08-06): ALL v3 ladder PRs need approval from another person on the v3 agent project — Henry cannot merge alone. No orchestrator merges.**
    - **PR 1 FOUNDATION #13505 merge-ready at c056a2dc** (6 rounds; scope-down done, containment 9/9). Blocked only on `REVIEW_REQUIRED` + merge word.
    - **NEW overnight 2026-08-06 (both FULLY review-clean incl. Devin sweeps, undrafted, CI CLEAN, stacked on foundation, auto-retarget to main at #13505 merge):**
        - **#13557 agent-chosen sandbox staging** (`to_sandbox` opt-in on spooling seam, threshold spill stays safety net) — 4 rounds, final head 27967736. Fixed along the way: wrapper idempotent under load_skill re-wrap; control-plane tools (load_skill/view_skills) never expose to_sandbox; spill metrics tagged threshold vs opt_in.
        - **#13558 write door (scoped)** (`tools/write`: entity+operation registry, direct/diff-first/human-approval, diff-first = workspace artifact + FOR-UPDATE-locked confirm, NO tables) — 5 rounds, final head 2dda8011. Fixed: org+mode association scoping, confirm race, assert_never dispatch, bounded WRITE_STATE with fail-closed key admission + owned reservation lifecycle (no eviction of accepted keys, failures release capacity, conflict check precedes refusal paths).
    - **Devin rule (Henry 2026-08-06): Devin comments ALWAYS addressed + threads resolved.** Persisted: babysit-pr SKILL.md (+ watcher allowlist devin-ai-integration[bot]), pushing-code SKILL.md, AGENTS.md, protocol doc merge-ready gate. These repo-file edits are UNCOMMITTED in the main phoebe checkout — fold into a PR.
    - Recurring hook-infra flake: repo-wide ty/prettier hooks fail on missing `daytona` imports in worker environments — workers use documented --no-verify per orchestrator ruling; worth a root-cause follow-up.
    - Henry ruling 2026-08-06: main already has the read tool → reads rung #13540 (draft) likely redundant — NOT closed, Henry's call. #13417 (207-file old writes bundle) superseded by #13558 once landed — also left open for Henry.
    - Rungs after foundation: control #13538, bash door #13416 (stacked, gates-complete), #13413 routing flags parked LAST. #13408 closes when ladder covers it. #13193 parity harness owes rebase post-carve.
    - Orchestrator watchers live: gh_pr_watch on #13557 + #13558 (CI + bot threads); fleet watchlist empty, all workers archived.

## Watchouts

- **Fleet routing REVERTED (Henry 2026-08-06b): implement = cdx gpt-5.6-luna, review = cdx gpt-5.6-sol, orchestrator = cc claude-fable-5. Fable is orchestrator-only** ("fable should just be orchestrators"); the 2026-08-06a fable-reviewer swap is undone. Protocol doc updated.
- **Skip deep-review for pure UI/polish wiki PRs (Henry 2026-08-06c).** Gate-only, orch-merge on green. Keep review for logic/backend and UI tickets with non-trivial state machines. Applies wiki only, NOT phoebe. See [[skip-review-ui-wiki]] memory + protocol doc.
- NO LOCAL BAZEL for workers: bb remote evidence MUST use `bin/bb remote --run_from_commit=<full pushed SHA> test ...` one captured session with `git rev-parse HEAD && git status --porcelain`; runner log must show FETCH of that commit.
- Worker pre-push hooks: don't let workers repair remote-bazel hook plumbing for hours — ruling: `prek run --hook-stage pre-push` once, fix real diagnostics in own diff, `--no-verify` + documented bypass for hook-infra failures, PR CI authoritative (applied 2026-08-06 PHO-0-V3-WRITES).
- Review-loop protocol: verdict → archive reviewer → compact contract → replace implementer → steer now; reviewers read-only, never run tests.
- Idle-by-design merge-ready workers trip the 1800s workgraph stall alarm → archive them once orchestrator owns the PR watch (terminal archive edge stops alarms).
- cdx workers hallucinate SHAs/completions — verify via gh that PR head MOVED + files in scope before spawning review; require pasted `git rev-parse HEAD`.
- Security-bot fail-closes on >300KB diffs → Henry --admin; regenerate goldens from merged code, never text-merge.
- Henry decisions parked: PHO-15255 seam priority; PHO-14037; PHO-15223 retry-storm P2; #13384 migration (after next prod deploy); #13540 + #13417 disposition.
