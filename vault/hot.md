---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-15
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-15 early: WIKI-119 MERGED (#93, 0b6f391), fleet EMPTY** — non-markdown file open: scoped read-only file API + CodeFilePane (shiki, line numbers, find-in-file). THREE gate iterations: sol round 1 caught 6 (symlink-alias bypass + TOCTOU class), round 2 caught descriptor-containment residual (HIGH) + exact-cap truncation (LOW), round 3 clean. Independent suite 368 OK env-clean at c4c3da4. WIKI-120 merged earlier (#92, ba018ae, terminal fidelity + scrollback search).
- **Provider content filters killed BOTH the orchestrator session AND sol mid-gate** (Claude AUP block + Codex cyberPolicy, ~01:24Z) — exploit-phrased security review text tripped them. Replacement orchestrator recovered via transcript+raw.jsonl, unblocked sol with a steer imposing neutral defensive phrasing (what code guarantees, not how to bypass; no payload/PoC text). **Doctrine: all security review briefs/verdicts/steers use defensive language.** Sol's status file sat stale "working" 3h while run was blocked — read_agent runtime_state / raw events, not status file alone.
- **WIKI-117 confirmed live during #93 gate**: pytest from an orchestrator session inherits `WIKI_AGENT_ROLE=orchestrator` → test_wiki_artifacts_server role test fails on untouched main. Independent reruns: `env -u WIKI_AGENT_ROLE`.
- **Open wiki tickets**: WIKI-112 (dead-archive playwright flake on main), WIKI-113 (runtime_card sync blocks supervisor ≤5s), WIKI-116/117/118, WIKI-106/107.
- **Wiki.app running #87/#88/#89 harness, E2E verified** (spawn→work→steer→archive green, no tmux). Untested: `wiki agent watch` autodiscovery. `wiki search` live — prefer over map.md-walk.
- **Phoebe backlog (fleet EMPTY)**: PHO-13759 (capability rename + wire-value audit), PHO-13737 (exe.dev sandbox, shaping), + SIX from Henry's 2026-07-15 roadmap: PHO-13760 deployment report pack (High), PHO-13761 shift-type labeling (Wayne gate), PHO-13762 DRI staleness ping, PHO-13763 daily per-org scratchpad, PHO-13764 onboarding plans (blocked by 13761), PHO-13765 agentic follow-ups (blocked by 13763). Recent merges: PHO-13733 (#11375 admin tool docs, 5 iterations), 13735/13736, evening five. Ticket auto-transition stalls at Merged — hand-bump (recurring).
- **Default pipeline locked (Henry 2026-07-14)**: Fable (cc) orchestrates → gpt-5.6-luna (cdx) implements → gpt-5.6-sol (cdx) reviews → findings route through Fable as structured steers until sol clean → merge per repo authority. In [[orchestrator-worker-protocol]]. NO iteration cap — iterate until clean.
- **`.codex/worktrees/` ~50 stale entries** — prune pending Henry. Stale worktrees: wiki-43-terminal-fidelity (unmerged ~500-line commit, ship or drop), wiki-41-native-surfaces + wiki-24-hidden-probe (dirty).
- **Phoebe carry-overs**: PHO-13646 rollup flag — watch first nightly pulse; revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets; parked race fix `b4d5e7f9` — PR or drop.

## Recent facts

- `wiki agent status` JSON sorts keys — extract fields with python json, never `cut -cN`. Arm-time self-verify monitors.
- Pin gate verification to the SHA under review, not worktree tip (Bugbot lesson, PHO-13736).
- Sidecar auto-rebuilds knowledge.db on schema mismatch — check `~/.wiki/knowledge.db.rebuilding` before index ops.
- Soft cap 5 LIVE workers, warning-only; spawn under bazel load is the killer.
- Bun at ~/.bun/bin NOT on headless-shell PATH — `export PATH="$HOME/.bun/bin:$PATH"` before phoebe pre-push.

## Watchouts

- Ship-shaped findings → ticket IMMEDIATELY.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Gate independently reruns suites; demand raw result lines in PR body.
