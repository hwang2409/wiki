---
type: reference
tags: [zeta, testing]
created: 2026-09-01
updated: 2026-09-09
---

# Live Home Test Pollution


## What happened

- The zeta test suite spawns tmux panes. The tmux server does not inherit the client's `ZETA_HOME`, so pane-spawned zeta processes write to the LIVE `~/.zeta`.
- Result: 2,010 test-created session dirs in `~/.zeta/sessions` dated 08-20 through 09-01, attributable by session `cwd` to every lane worktree AND plain main checkouts.
- Every full-suite run on main (current head 84545ad) creates ~2 live sessions. The leak is ON MAIN — it predates PR #79, which merely surfaced it (found by ZETA-49-REVIEW1, 2026-09-01).
- Same hazard class as the ZETA-44 round-2 live-history finding, but a different vector (tmux env forwarding vs Path.home pinning).

## State

- Fix contracted to ZETA-49-PR2 (PR #79): pass env inside the pane command + attribution-safe isolation assertion.
- PR #78 (ZETA-44) still pollutes during its gates until it carries the fix (rebase after #79 merges, or vice versa).
- Debris cleanup pending Henry's decision — deleting from live `~/.zeta/sessions` is destructive; real dogfooding sessions share the dir (also cwd=repo root, so cwd alone cannot distinguish).

## Rule

- Orchestrator gate/baseline suite runs on zeta also pollute until the fix merges. Accept the ~2-session cost per run; clean up once after the fix lands.

## Resolution (2026-09-01)

- Fix merged in PR #79 (ZETA-49, squash bae2ddbe): pane commands carry the env; isolation asserted per-pane.
- Verified: post-merge main suite runs create ZERO live sessions (checked after #79 and #80 merges).
- Remaining: ~2,094 debris session dirs in live `~/.zeta/sessions` await Henry's cleanup decision.

## Cleanup (2026-09-04, Henry-authorized)

- 2,026 debris session dirs deleted from live `~/.zeta/sessions` (fake-provider + worktree-cwd classification); 71 real/pre-meta sessions kept.
- Reversible: tarball at `~/me/fun/agent-archive/zeta-session-debris-20260904.tar.gz` (1.0M).
- The three main-checkout quarantine stashes dropped; `zeta_test.txt` deleted. Thread closed.

## Guard hardened (2026-09-04, ZETA-66)

- Teardown guard rebuilt attribution-safe over 7 review rounds (PR #102, squash f69a389); rounds 6-7 fixed by orch direct commits after workers dodged the exact-match mandate twice.
- Final semantics: session-scoped marker/inode attribution survives setsid()/tmux; external writes need declarations with exact expected bytes, validated as an ordered per-path content chain, consumed all-or-none; declared creations tolerate only implied parent-dir timestamp churn; everything unattributed fails closed.
- Round-7 reviewer ran the 15-probe matrix 11x: zero false positives, zero false negatives; guard certified deterministic.
- CONSEQUENCE: the standing "rerun the ZETA-44 teardown-guard test once" kickoff clause is RETIRED. A guard failure now means a real leak (or an undeclared genuine external write) - investigate, never rerun-and-ignore.

## Residue bricked the GUI (2026-09-09, zeta orch)

- 19 of the kept session dirs had `conversation.jsonl` but no `meta.json` (pre-meta keeps + partial creates, all dated 08-20).
- `SessionManager.list_sessions` `_read()`s every dir and raises on a missing `meta.json`, so ONE such dir failed the whole listing with a misleading "session ... was not found" (-32602).
- The gpui GUI (new since #130) calls list_sessions at connect; the error escaped as fatal `Lost`, reconnect re-hit it: GUI fully bricked (empty sidebar, no new session, no send). Worker gates never saw it - they isolate ZETA_HOME, so only real `~/.zeta` had the trigger (same blindness class as the env-sensitive-tests lesson).
- Unblock: 19 dirs quarantined to `/tmp/zeta-quarantine/` (nothing deleted). Durable fix MERGED: ZETA-96 (PR #135, squash 2ce20d4, 2026-09-09, 10 review rounds). Scope grew review-driven: resilient listings, depth-bounded JSON loading, per-session error boundary, atomic no-replace publication, GUI RPC degradation, checkpoint corruption fail-safe with shared restorability resolver.
