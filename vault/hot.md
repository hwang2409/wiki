---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-06
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI (orch `wiki-dev`)**: OpenCode UI arc complete 2026-08-06 (13 merges thru #193/WIKI-257). **NEW bug-fix arc (Henry screenshots, eve 2026-08-06): four cc opus-4.7 implementers live, all gate-only (no reviewers, orch merges on green): WIKI-258 split-diff columns collapse to 1ch (`.wiki-diff` table-layout fixed + overflow-wrap anywhere, styles.css ~11730), WIKI-259 inconsistent transcript row gaps (virtualized height cache, session.tsx VirtualSessionRow), WIKI-260 font picker enumerates real installed families — no variant grouping, JetBrains Mono ≠ Nerd Font (backend /api/fonts; WKWebView lacks queryLocalFonts), WIKI-261 highlight numbered read-tool outputs (strip cat-n gutter, tokenize, gutter column; guard at session.tsx ~1662).** Screenshots in vault/wiki-app/assets/wiki-25[89]*/26[01]*. Watches armed on all four. Parked: store-pruning P2, worktree-debris P3 (~95 stale), artifact-rendering arc, WIKI-233 reds triage, WIKI-228/229.
- **PHOEBE (orch `phoebe-dev`) — v3 ladder arc, HENRY WENT HANDS-ON 2026-08-06 afternoon:**
    - **Henry pushed directly to `henry/v3-ladder-1-foundation`** (merged main in; commit 055dc855 DELETED the `spooling/` package, folded modules back into `middleware/`; cbc8b7af removed `audit_conversation_id` from the v3 workspace path).
    - **#13505 FOUNDATION: APPROVED + round-8 delta review passed at cbc8b7af.** CI re-running (4 checks pending). Merge word = Henry only (v3 ladder needs second-person approval; no orchestrator merges).
    - **#13558 write door: CLOSED un-merged by Henry 18:54Z, no comment. Do not reopen or rework without his word.** Its content + #13417 disposition = Henry's call.
    - **#13557 sandbox staging: APPROVED but DIRTY** after Henry's foundation restructure — semantic port needed (feature written against deleted `spooling/` seam). `PHO-0-V3-OPTIN-REBASE` cdx-luna worker live in `.worktrees/v3-sandbox-optin`: merge foundation in, port to_sandbox onto `middleware/*`, preserve reviewed invariants (opt-in on spool seam, idempotent wrapper, control-plane tools never expose to_sandbox, threshold-vs-opt_in metric tags).
    - #13587 TUI polish: draft, CLEAN on foundation, parked.
    - **PHO-15330 monitoring REDESIGNED (Henry 2026-08-06 eve): v2 sandbox-guard design (#13598, closed) replaced by one-shot polling tool `start_monitor(tool_name, params, wake_states, polling_interval_s, timeout_s)`** — host polls a registry-flagged read-only tool, wakes agent via #13530 poke machinery on state match or timeout; no cancel, no sandbox evaluator, no long-lived-process-API prerequisite. Luna worker live: rewrites DESIGN.md v3 + implements, new PR onto foundation.
    - Watchers live (replacement orch, prior session rate-limited dead ~20:10Z): gh_pr_watch summary monitors on #13505 + #13557, worker status monitor. Wrapper: /tmp/agent-status/pr_watch_summary.sh.
    - Devin rule (Henry): Devin comments ALWAYS addressed + threads resolved. Persisted to babysit-pr/pushing-code SKILL.md, AGENTS.md, protocol doc — those edits still UNCOMMITTED in main checkout (plus unexplained revenue_accounts edits, likely Henry's; don't touch). Fold skill edits into a PR later.
    - Rungs after foundation: control #13538, bash door #13416 (stacked, gates-complete), #13413 routing flags LAST. #13408 closes when ladder covers it. #13193 parity harness owes rebase post-carve. #13540 reads rung: Henry's call.
    - Hook-infra flake persists: ty/prettier hooks fail on missing `daytona` imports → documented --no-verify allowed.

## Watchouts

- **Fleet routing (Henry 2026-08-06b): implement = cdx gpt-5.6-luna, review = cdx gpt-5.6-sol, orchestrator = cc claude-fable-5. Fable is orchestrator-only.** **Frontend-design tickets: cc opus-4.7 implementers, NOT fable — fable too expensive (Henry 2026-08-06d); detailed orchestrator-written tickets compensate. Protocol doc updated.**
- **Skip deep-review for pure UI/polish wiki PRs (Henry 2026-08-06c).** Wiki only, NOT phoebe. See [[skip-review-ui-wiki]].
- NO LOCAL BAZEL for workers: bb remote evidence via `bin/bb remote --run_from_commit=<full SHA> test ...` with pasted `git rev-parse HEAD && git status --porcelain`.
- Worker pre-push hooks: `prek run --hook-stage pre-push` once, fix real diagnostics, --no-verify + documented bypass for hook-infra failures; PR CI authoritative.
- Review-loop: verdict → archive reviewer → compact contract → replace implementer → steer now; reviewers read-only, never run tests.
- cdx workers hallucinate SHAs — verify via gh that PR head MOVED before spawning review.
- Security-bot fail-closes on >300KB diffs → Henry --admin; regenerate goldens from merged code.
- Henry decisions parked: PHO-15255 seam priority; PHO-14037; PHO-15223 retry-storm P2; #13384 migration (after next prod deploy); #13540 + #13417 + #13558 disposition.
