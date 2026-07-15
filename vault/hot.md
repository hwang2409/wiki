---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-15
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-15 midday: WIKI-123 MERGED (#94, 0b6e9ad), wiki fleet EMPTY** — file API re-rooted vault→repo (FILES_ROOT), so Show-all-files now exposes real source tree. Root cause of Henry's "can't see code-editor features": WIKI-119 was vault-scoped and vault has zero non-md files — toggle was a visible no-op. Two-round sol gate (r1: repo-md misrouted to note API, non-vault new-note/drop mistargeted, dot/empty segment rejection missing; r2 clean at d7b5100). Independent rerun 381+73 subtests green. Native bundle rebuilt 11:22 local — **Henry must relaunch Wiki.app to load new sidecar** (orchestrators die on relaunch; recovery via runtime card works). WIKI-119 (#93) + WIKI-120 (#92) merged earlier same arc.
- **Ticket ID collision hit**: two sessions both allocated WIKI-123 (repo-file-browsing + svg-not-rendered bug). Renumbered svg bug → **WIKI-124** (todo P2: 24KB svg accepted server-side, never displayed; 800B fine; bisect threshold, check mermaid compact-preview overlap). Watchout: check todo.md for ID collisions before filing; `wiki todo complete` refuses ambiguous substring — silent no-op, verify after.
- **Code-editor arc wave 2 queued**: WIKI-121 (CodeMirror 6 editor, P1), WIKI-122 (explorer polish: file icons, fuzzy path in Cmd+K, recents). Standing Henry mandate: drive arc autonomously, default pipeline, no iteration caps.
- **Open wiki tickets**: WIKI-112 (dead-archive playwright flake), WIKI-113 (runtime_card sync blocks supervisor ≤5s), WIKI-116/117/118, WIKI-106/107, WIKI-124.
- **Security review doctrine**: all briefs/verdicts/steers use defensive language (what code guarantees, no payload/PoC text) — provider content filters killed an orchestrator + sol mid-gate 07-15 ~01:24Z. Status files can sit stale "working" while run is blocked — read runtime_state/raw events too.
- **Default pipeline locked (Henry 2026-07-14)**: Fable (cc) orchestrates → gpt-5.6-luna (cdx) implements → gpt-5.6-sol (cdx) reviews → findings route through Fable as structured steers until sol clean → merge per repo authority. In [[orchestrator-worker-protocol]]. NO iteration cap.
- **Phoebe (own orch live)**: PHO-13762/13763 in flight (rebase/force-push recovery after codex app-server restarts), PHO-13760 merged (#11384). Backlog: PHO-13759, 13737, 13761 (Wayne gate), 13764/13765 (blocked). Ticket auto-transition stalls at Merged — hand-bump.
- **`.codex/worktrees/` ~50 stale entries** — prune pending Henry. Stale: wiki-43-terminal-fidelity (unmerged ~500-line commit), wiki-41-native-surfaces + wiki-24-hidden-probe (dirty).
- **Phoebe carry-overs**: PHO-13646 rollup flag nightly pulse; revoke 6 `ADMIN_AGENT_SNOWFLAKE_*` prod secrets; parked race fix `b4d5e7f9` — PR or drop.

## Recent facts

- WIKI-117 live: orchestrator-inherited `WIKI_AGENT_ROLE` breaks a main test — independent reruns `env -u WIKI_AGENT_ROLE`; repo venv python at `.venv/bin/python` (bare python3 lacks pytest).
- `wiki agent watch --until merge-ready` false-fires on stale pre-steer merge-ready status — guard with until-working loop first, then re-watch. Arm-time self-verify all monitors.
- Backend serves frontend live from repo `frontend/dist` (rebuild → reload works), but backend code itself runs from bundled sidecar — API changes need `make native-build` + app relaunch.
- Pin gate verification to the SHA under review, not worktree tip.
- Soft cap 5 LIVE workers, warning-only.
- Bun at ~/.bun/bin NOT on headless-shell PATH — export before phoebe pre-push.

## Watchouts

- Ship-shaped findings → ticket IMMEDIATELY (check ID collisions first).
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths.
- Schema-touching phoebe PRs: `migrate apply` locally; catalog conflicts → regenerate.
- Gate independently reruns suites; demand raw result lines in PR body.
