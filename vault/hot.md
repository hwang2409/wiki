---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-14
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **Wiki.app RELAUNCHED on new build 2026-07-14 14:01 — #81-#86 all live, verified**: new supervisor holds lock (WIKI-91 daemon swap done), `/api/agents` 83ms (#85 snapshot serving confirmed), sidecar port 8213. Relaunch killed pre-existing tmux-registered sessions; supervisor respawned orchestrators headless (wiki session 7 = replacement orchestrator, phoebe session 6).
- **knowledge.db rebuilt post-diet + VACUUMed: 2.0G → 1.0G** (schema v2, built 14:07). Sidecar auto-rebuilt on schema mismatch at startup (~4min); orchestrator VACUUMed manually (9s). Live content ~1.05G: chunks 459M, events 277M, FTS 111M — ~400M diet target was optimistic, actual cut ~50%. `wiki search` verified working.
- **New tickets from relaunch**: WIKI-106 (rebuild never VACUUMs — free pages stay on disk; also CLI rebuild racing sidecar auto-rebuild dies with bare "database is locked", busy_timeout 750ms), WIKI-107 (native_server lost-single-instance-race logs full PyInstaller tracebacks every relaunch — should exit 0 quietly).
- **Six-ticket arc shipped earlier today** — WIKI-100 (#82) knowledge layer v1 (**agents: prefer `wiki search` over map.md-walk**; spec docs/superpowers/specs/2026-07-14-knowledge-layer-storage-design.md); WIKI-101 (#81) rejected-badge; WIKI-102 (#83) index diet SCHEMA_VERSION=2; WIKI-103 (#84) orchestration CLI (`wiki agent watch`/`wiki gate`/`todo complete --done-line`); WIKI-104 (#85) supervisor RPC; WIKI-105 (#86) font weight picker. Gate loops caught 8 BLOCKING + 4 HIGH.
- **Backlog**: v1.5 knowledge analytics, v2 embeddings, native thin-runner idea (not ticketed), WIKI-88 orchestrator artifact registration (open).
- **Phoebe (2026-07-14)**: PHO-13646 merged (#11271, `ACCOUNT_HEALTH_BATCH_ROLLUP_ENABLED` — enable + watch first nightly pulse); PHO-13665/13666 merged (#11307/#11311); PHO-13669/13676/13690 workers died in relaunch (registry state=dead) — phoebe orchestrator to resume/respawn. Post-#11222 ops pending: revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets. Parked race fix `b4d5e7f9` (`henry/staging-modal-deploy-investigation-2`) — PR or drop.
- **Uncommitted in wiki repo**: phoebe orchestrator's vault delta (done.md PHO-13665/13666, todo prune) — left for phoebe session to commit; frontend/package-lock.json engines-key reorder (npm normalization noise).

## Recent facts

- Sidecar auto-rebuilds knowledge.db on schema mismatch at startup — don't run `wiki index rebuild` right after relaunch, it races and dies on lock. Check `~/.wiki/knowledge.db.rebuilding` marker first.
- Gate doctrine: workers make FALSE green-suite claims (2x WIKI-104) — gate independently reruns suites; demand raw result lines in PR body.
- Codex auth: refresh-token revocation blocks NEW thread starts only; fix = `codex logout && codex login`, then `/replace`.
- Worker soft cap 5/box real (10 workers + bazel = spawn death).
- Stale worktrees pending Henry: `wiki-43-terminal-fidelity` (unmerged ~500-line commit, no PR — ship or drop), `wiki-41-native-surfaces` + `wiki-24-hidden-probe` (dirty).

## Watchouts

- Sibling workers same file surface: overlap note in both prompts; second-to-merge rebases.
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Ship-shaped findings → ticket IMMEDIATELY.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
