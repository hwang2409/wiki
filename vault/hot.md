---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-16
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-16: WAVE 4b COMPLETE** — WIKI-117+118 merged (#103, squash 8f68d53, 4 sol review rounds): test-env hygiene (role tests pin WIKI_AGENT_ROLE/WIKI_AGENT_ID) + all five #91 hardening items, every fix mutation-verified. Arc survived two codex auth revocations + an app restart mid-gate. Waves 3+4 earlier same night: WIKI-127 (#98), WIKI-128 (#99), WIKI-116+124 (#100), WIKI-112 (#101), WIKI-129 (#102).
- **NEW SIDECAR REBUILD DEBT**: #103 backend changes (store.py, transcripts.py, wiki_artifacts.py) landed AFTER the WIKI-130 swap — deployed sidecar doesn't have them. Test-focused, low urgency; fold into next swap (staged flow: `FORCE_STAGE_ONLY=1` while app open, SIGTERM supervisor → swap-native-app.sh → relaunch).
- **WIKI-130 DEPLOYED 2026-07-15 ~21:10**: launch regression fixed (`.native-build-staging` ancestor marker, first Rust unit tests). Safe deploy flow proven; fleet auto-resumed via `recover_on_start`. 127 endpoints, 116 dedupe, 129 app.lock guard all live.
- **Open design threads**: (1) daemon-ize backend (launchd) — Henry interested, unticketed, next big structural win; (2) WIKI-126 rebrand BLOCKED on name pick; (3) cross-workspace switcher, file editing, semantic search (sqlite-vec) — unticketed.
- **Open wiki tickets**: compact-mermaid-preview (unfiled), WIKI-92/WIKI-109 deterministic frontend test failures at main (candidates for filing), WIKI-94 latency guard flake (candidate ticket).
- **Stale blocked cdx workers under misc orch**: PUF-8 (dead-token 401) + PUF-2-REVIEW2 (worktree gone) — misc orch's to clean; their stale processes are prime suspects for codex auth chain re-revocation.
- **Phoebe**: PHO-13804/13815 workers under phoebe orch (separate).

## Recent facts

- **Codex refresh token revoked TWICE 2026-07-15/16** — stale blocked workers holding old tokens suspected of poisoning the rotation chain on auto-resume. After re-auth: replace_agent every live cdx worker so all processes hold fresh tokens; kill/archive stale blocked ones FIRST. Probe auth cheaply: `codex exec --skip-git-repo-check -m gpt-5.4-mini "Reply OK"`.
- Workers survive backend restarts (own MCP sidecar, in-memory session) — status file `/tmp/agent-status/<T>.json` stays readable when backend down; monitors need CLI→file fallback.
- Reviewer state field unreliable — verdict step text authoritative, always.
- Supervisor MCP ops can 1s-timeout under load — verify effect before retrying steers.
- `wiki` CLI: log-done + todo add/move/complete; not on monitor-shell PATH — use `~/me/fun/wiki/wiki` absolute.
- cdx spawns can hang at provider startup (no status file) — startup-hang check in monitor template; fix via replace_agent.

## Watchouts

- Next free ticket ID: WIKI-131.
- Pin review worktrees + gates to SHA (`--match-head-commit` on merge, worktree add at pinned SHA).
- Local main can hold unpushed vault/ticket commits — `git pull --rebase` (stash dirty vault notes first), then push.
- Stop BOTH worker+reviewer monitors at wrap-up; archive reviewer immediately after verdict routed.
- Workers' PR bodies can overclaim — reviewers verify claims by mutation, keep ordering it.
- `.codex/worktrees/` ~40 stale entries from older arcs — prune pending Henry (wave 4b's cleaned).
