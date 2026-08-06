---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: WIKI-244 MERGED (#178, 2026-08-06). 2026-08-05 hotfixes landed direct to main (3c180a3 orphan-sweep once-per-boot, a0b4f0c agent-prose monospace, c1c86e2 build-script venv python). **OpenCode design arc (Henry 2026-08-06: adopt OpenCode TUI design, not harness)**: doctrine extracted first-hand -> [[opencode-design-direction]] + [[opencode-tui-design-notes]] (21 web-transfer rules; clone at /tmp/opencode-ref). Fleet: **WIKI-245** (transcript two-tier redesign, steered to doctrine, TOP priority), **WIKI-246** (app chrome: elevation/left-bar/dialogs/footer/composer/motion), **#179** (WIKI-242 a11y, REVIEW1 running), **#180** (WIKI-227 costs scan + payload slim, REVIEW1 running); both implementers archived, orchestrator owns PR watches, merges after clean gate (wiki repo authority). Monitor armed off `.wiki-dev-watchlist`. Boot-sweep follow-up (terminal-run skip + store pruning) = P2 todo. Merge-order watchout: #179/#180 -> #245 -> #246 (styles.css overlap; later PRs rebase).
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

- NO LOCAL BAZEL for workers: bb remote evidence MUST use `bin/bb remote --run_from_commit=<full pushed SHA> test ...` one captured session with `git rev-parse HEAD && git status --porcelain`; runner log must show FETCH of that commit.
- Worker pre-push hooks: don't let workers repair remote-bazel hook plumbing for hours — ruling: `prek run --hook-stage pre-push` once, fix real diagnostics in own diff, `--no-verify` + documented bypass for hook-infra failures, PR CI authoritative (applied 2026-08-06 PHO-0-V3-WRITES).
- Review-loop protocol: verdict → archive reviewer → compact contract → replace implementer → steer now; reviewers read-only, never run tests.
- Idle-by-design merge-ready workers trip the 1800s workgraph stall alarm → archive them once orchestrator owns the PR watch (terminal archive edge stops alarms).
- cdx workers hallucinate SHAs/completions — verify via gh that PR head MOVED + files in scope before spawning review; require pasted `git rev-parse HEAD`.
- Security-bot fail-closes on >300KB diffs → Henry --admin; regenerate goldens from merged code, never text-merge.
- Henry decisions parked: PHO-15255 seam priority; PHO-14037; PHO-15223 retry-storm P2; #13384 migration (after next prod deploy); #13540 + #13417 disposition.
