---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-14
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **SIX wiki tickets shipped 2026-07-14, fleet EMPTY** — WIKI-100 (#82) knowledge layer v1 (SQLite FTS index, `wiki search`/`wiki links`/`wiki index rebuild` + `search_knowledge` MCP; **agents: prefer `wiki search` over map.md-walk for recall**; spec docs/superpowers/specs/2026-07-14-knowledge-layer-storage-design.md); WIKI-101 (#81) cc render_artifact rejected-badge fix; WIKI-102 (#83) index diet (excerpt chunks, line-level base64/ANSI filters, opt-in legacy sweep, SCHEMA_VERSION=2); WIKI-103 (#84) orchestration CLI (`wiki agent watch` deduped events + `wiki gate` verdict JSON + `todo complete --done-line`); WIKI-104 (#85) supervisor RPC responsiveness (snapshot-served /api/agents, idempotent spawn/message request_id, fast-read-allowlist timeouts, soft cap 5); WIKI-105 (#86) font weight picker. All squash-merged by orchestrator after adversarial gate loops (2-3 iterations each; 8 BLOCKING + 4 HIGH caught total, all revert-tested).
- **Wiki.app bundle STALE vs main — rebuild+relaunch pending (Henry)**: running sidecar/daemon predate #81-#86. `make native-build` + relaunch picks up ALL of it (incl. supervisor daemon swap → WIKI-91 auto-migration). CLI verbs (`wiki search/gate/agent watch/index`) work from repo checkout NOW.
- **knowledge.db live**: first full rebuild done (1.9G pre-diet). Post-#83 next `wiki index rebuild` shrinks it (~400M target) — legacy agent-archive now opt-in `--include-legacy`.
- **Backlog seeded from today**: v1.5 knowledge analytics, v2 embeddings, native thin-runner idea (single-turn LLM jobs: TIL extraction, hot.md drafts — discussed, not ticketed), WIKI-88 orchestrator artifact registration (still open; orchestrator artifact today worked only via user-level MCP leak).
- **Phoebe (2026-07-14)**: PHO-13646 merged (#11271) — batch account-health rollups behind `ACCOUNT_HEALTH_BATCH_ROLLUP_ENABLED` (enable + watch first nightly pulse); PHO-13647 open. Post-#11222 ops pending: revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets. Unpushed race fix parked `b4d5e7f9` (`henry/staging-modal-deploy-investigation-2`) — PR or drop.

## Recent facts

- Gate-loop yield today: every PR had BLOCKING/HIGH findings invisible to CI-green + threads-clear (broken codex approvals, --until-merged infinite loop, layout regression visible in worker's OWN screenshots, control_attached pid-inference vs types.py:352 doctrine). Two workers made FALSE green-suite claims (WIKI-104, twice) — gate independently reruns suites now; demand raw result lines in PR body.
- Codex auth: refresh-token revocation blocks NEW thread starts only (live sessions keep session tokens); fix = `codex logout && codex login`, then `/replace` blocked runs.
- Supervisor RPC under load-35: responses time out client-side while ops land server-side — ALWAYS ground-truth-check (registry/raw.jsonl) before retry. Fixed properly by #85 post-relaunch.
- Worker-count soft cap 5/box real: 10 concurrent terra workers + bazel = thread/start spawn death (killed WIKI-103 run 1).
- `wiki todo complete` now removes-only by default; `--done-line "<terse>"` for the log line.
- Stale worktrees kept pending Henry: `wiki-43-terminal-fidelity` (unmerged ~500-line commit, no PR — ship or drop), `wiki-41-native-surfaces` + `wiki-24-hidden-probe` (dirty).

## Watchouts

- Sibling workers same file surface: overlap note in both prompts; second-to-merge rebases (proven 3x today).
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Ship-shaped findings → ticket IMMEDIATELY.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
