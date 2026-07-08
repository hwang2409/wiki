---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-08
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki NATIVE APP MERGED 2026-07-08** (PR #1, `b658a30`): Tauri 2 shell + PyInstaller onefile sidecar (`wiki-backend`) serving /api + built frontend on dynamic loopback port; lifecycle = spawn→health-wait→one-restart→kill-on-quit + parent-pid watchdog; `make native-build` → `src-tauri/target/release/bundle/macos/Wiki.app`. Web flow (:8011/:5173) unregressed. Manual QA checklist for WKWebView-only deltas in PR #1 body — Henry to click through. Research [[wiki-native-app-research]], plan+parity ledger in agent-archive/WIKI-2.
- **wiki app (web)**: agent-first monitor — /agents fleet, sidebar agents mode, #/agent/<id> full transcripts (codex+claude), tmux-style `Ctrl+A` leader/status bar for orchestrator groups, composer (vim, images inline [Image #N], skill autocomplete, on-idle queue), subagent inspect, split agent panes, settings (13 mono fonts + size sliders). [[wiki-app-ui-direction]], protocol [[orchestrator-worker-protocol]].
- **phoebe admin-agent**: run-data verb family shipped 2026-07-07 (5 merges); PHO-13157 held for team-lead review; PHO-13138 terraform-parked. See [[admin-agent]].

## Recent facts

- Orchestration pattern proven end-to-end on WIKI-2: plan worker (gpt-5.5 xhigh) → gate → implement worker (gpt-5.4 xhigh) → plan-dispute steer (push local main first!) → parity steers → independent review subagent → fix round → merge. Internal tickets use fake IDs with digits (WIKI-1/WIKI-2) so transcript resolver maps them.
- `wiki agent orch/register --orch/update/done --outcome` full protocol in daily use; wrap-up order archive→done→delete.
- Native gotchas learned: reqwest::blocking panics inside tauri async runtime (use std::thread); PyInstaller onefile needs parent-pid watchdog + force_exit fallback; hardenedRuntime must be false for ad-hoc-signed frozen sidecars; WKWebView AX tree degrades after repeated relaunches (drives skips → verify prod build via browser against sidecar port instead).

## Watchouts

- Local main must be PUSHED before spawning workers (worktrees come from origin/main) — caused WIKI-2 plan-dispute.
- Agents rewrite todo.md/map.md concurrently — refetch before line surgery.
- Codex/Claude transcript JSONL formats are unversioned internals — wiki parsers drift with CLI updates.
- NEVER `pkill -f` on substrings appearing in worker prompt text — match binary paths.
