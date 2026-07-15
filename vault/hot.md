---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-15
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-15 night: WAVE 3 COMPLETE, wiki fleet EMPTY** — WIKI-127 multi-workspace file browsing (#98, 6 review rounds) + WIKI-128 code-viewer density (#99, 3 rounds) merged; main at 50e5917. Post-crash recovery worked: workers respawned in preserved worktrees with crash-resume brief addendum, lost no work.
- **NEEDS HENRY: app relaunch for 127 backend** — 127 added backend endpoints (/api/workspaces, workspace param); live sidecar runs OLD backend. Frontend dist rebuilt (degrades gracefully to wiki-only until relaunch). Sequence per WIKI-129 gotcha: QUIT Wiki.app first → `make native-build` → relaunch. Never build under running app (kills supervisor; orch session dies with quit — status file carries resume state).
- **127 security arc (6 rounds, sol reviewer earned keep)**: root-swap TOCTOU → fd-pinned roots; fd-lifetime bugged rounds 2-3 → orchestrator issued design directive (constant-descriptor walk: queue relpath tuples, reopen from root fd) → clean after. 121 lesson reconfirmed: mechanism bugs 2+ rounds → order redesign, don't patch.
- **Open design threads (Henry engaged, unticketed)**: (1) daemon-ize backend (launchd; kills relaunch-fleet-wipe + WIKI-129 class) — Henry interested; (2) WIKI-126 surface rebrand BLOCKED on Henry name pick; (3) WIKI-129 native-build staging-dir/atomic-swap fix — ticketed, unstarted.
- **Open wiki tickets**: WIKI-112 (dead-archive playwright flake — bit every review round again this wave; worth prioritizing), WIKI-113, WIKI-116/117/118, WIKI-106/107, WIKI-124 (svg threshold — maybe fixed by #97, verify first), WIKI-129. Also WIKI-92/WIKI-109 frontend tests fail deterministically at main — untracked, ticket if Henry wants.
- **Phoebe**: PHO-13804 (PR 11420, 2 review rounds so far) + PHO-13815 workers active under phoebe orch.

## Recent facts

- Reviewer state field unreliable both directions (merge-ready state w/ NOT-MERGE-READY text and reverse) — verdict step text authoritative, always.
- cdx spawns can hang at provider startup under fleet load (raw.jsonl stalls at MCP-startup events, no status file ever) — detect via missing status file ~10min, fix via replace_agent. Monitor template now includes startup-hang check.
- Worker staleness alarms false-fire on idle merge-ready workers awaiting review — check runtime_state=idle before panicking.
- `wiki` CLI: `log-done` + `todo complete` (no `done` subcommand); todo complete doesn't auto-write done.md line.
- Backend serves frontend live from frontend/dist (rebuild → reload); backend code needs native-build + relaunch.

## Watchouts

- Next free ticket ID: WIKI-130 (check todo.md collisions before filing).
- Pin review worktrees + gate verification to SHA under review; `--expect-sha` on every gate.
- After merges: checkout package-lock.json before pull (generated-file churn blocks pull).
- Stop BOTH worker and reviewer monitors at ticket wrap-up (clean this wave — keep it up).
- `.codex/worktrees/` still has ~40 stale entries from older arcs — prune pending Henry (this wave's 11 all cleaned).
