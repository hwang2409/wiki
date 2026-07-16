---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-16
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-16 early: WAVE 4 COMPLETE** — WIKI-112 flake fix (#101, 2 rounds), WIKI-116+124 artifact render trust (#100, 5 rounds), WIKI-129 native-build safety (#102, 4 rounds) all merged; main at ef261ac. Wave 3 earlier same night: WIKI-127 (#98) + WIKI-128 (#99). Henry AFK ("up to you") — orchestrator running approved queue autonomously.
- **WAVE 4b LIVE**: WIKI-117+118 bundled (test-env hygiene + #91 hardening follow-ups) — check todo.md In Progress for worker state.
- **WIKI-130 FIXED (1b1cacb), SWAP PENDING**: WIKI-129 baked staging path into `resolve_repo_dir()` (compile-time CARGO_MANIFEST_DIR) → launch failed "os error 2". Fix: `.native-build-staging` ancestor marker like `.codex` + first Rust unit tests. Fixed bundle STAGED at `.native-build-staging/20260715-210420-54992`. Swap needs supervisor stopped — supervisor parents ALL workers/orchestrators (killing it kills fleet). When fleet idle: quit app, stop supervisor, `./scripts/swap-native-app.sh <stage_root>`, relaunch. Stacked backend changes (127 endpoints, 116 dedupe, 129 lock) deploy with same swap. Backend changes stacked awaiting native rebuild: 127 workspace endpoints, 116 diagnostic dedupe (store.py), 129 app.lock acquisition (guard protection only fully active once rebuilt GUI/sidecar deployed). New build flow post-129: staged + atomic, refuses under running app, `FORCE_STAGE_ONLY=1` to prepare while app open. Frontend dist current (rebuilt after each merge).
- **Review-loop quality this arc**: sol reviewers reproduced every claimed fix, caught fake regression tests (124 test passing pre-fix), prompt-injection channel in diagnostics, GUI-vs-sidecar lock scoping. Pattern held: converging finding counts each round; design directives (constant-fd walk, GUI-owned lock, feedback-loop architecture) beat cleanup patches.
- **Open design threads**: (1) daemon-ize backend (launchd) — Henry interested, unticketed, next big structural win; (2) WIKI-126 rebrand BLOCKED on name pick; (3) cross-workspace switcher, file editing, semantic search (sqlite-vec) — surfaced in improvements discussion 2026-07-15, unticketed.
- **Open wiki tickets**: WIKI-130 swap step (P1, fleet-idle gated), WIKI-117/118 (wave 4b), compact-mermaid-preview (unfiled), WIKI-92/WIKI-109 deterministic frontend test failures at main (untracked — candidates for filing), WIKI-94 latency guard flaked 4x under host load (candidate ticket).
- **Phoebe**: PHO-13804/13815 workers under phoebe orch (separate).

## Recent facts

- Reviewer state field unreliable — verdict step text authoritative, always.
- cdx spawns can hang at provider startup under fleet load (no status file ever) — startup-hang check in monitor template; fix via replace_agent.
- Supervisor MCP ops can 1s-timeout under 14-worker load — verify effect (read_agent/pending) before retrying steers.
- Worker staleness alarms false-fire on idle merge-ready workers — check runtime_state first.
- `wiki` CLI: log-done + todo add/move/complete; todo section arg is "In Progress" (exact); todo complete needs unique substring.
- Artifact diagnostics (116) reach agents as user messages — normalized metadata only, injection-tested.

## Watchouts

- Next free ticket ID: WIKI-131.
- Pin review worktrees + gates to SHA under review (`--expect-sha`).
- checkout package-lock.json before pull after merges.
- Stop BOTH worker+reviewer monitors at wrap-up; archive reviewer immediately after verdict routed.
- Workers' PR bodies can overclaim (116 claimed a fix it hadn't made) — reviewers verify claims, keep ordering it.
- `.codex/worktrees/` ~40 stale entries from older arcs — prune pending Henry (waves 3+4's 25 all cleaned).
