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
- **phoebe mastermind arc 07-07/08: NINE merges** — 13142 charts, 13160+13165 Slack linking, 13146 python snippets, 13153 read/slice/diff, 13172 Twilio error visibility, 13074 error taxonomy + silent-down alerting, 13144 WellSky clock-in writeback (urgent incident; prod tunnel → `action_family_disabled` gate). Follow-up PHO-13207 (QA family-auth unification + historical-replay product call). See [[admin-agent]].
- **PHO-13206 tool audit DELIVERED** (report on ticket): 112 tools, 44 used/7d, 73.1% success. P0s awaiting Henry's call: split search_call_recordings (48% ok); Core account owner/filter/pagination. Validated: 11274/75, Logfire hardening, 11267. Killed: Snowflake trends, full texQL, broad playbooks.
- **PHO-13157 (#10751)**: gates passed, HELD for team-lead review; rebasing onto moved main. **PHO-13138 (#10716)**: terraform-parked.

## Recent facts

- Orchestration pattern proven end-to-end on WIKI-2: plan worker (gpt-5.5 xhigh) → gate → implement worker (gpt-5.4 xhigh) → plan-dispute steer (push local main first!) → parity steers → independent review subagent → fix round → merge. Internal tickets use fake IDs with digits (WIKI-1/WIKI-2) so transcript resolver maps them.
- check-aliveness skill: revived 2 stalled workers first poke. False positive: orchestrator panes QUOTING error strings classify STALLED — spinner should beat signature grep.
- Prod tunnel flow: `aws sso login --profile phoebe-production-admin` (plain `aws login` fails); SSM port-forward needs AWS_PROFILE+region; backgrounded shells can miss fresh SSO creds — nohup from foreground. Workers request rows via BLOCKED: prod-db-evidence; DSN never given to workers.
- Native gotchas: reqwest::blocking panics in tauri async runtime (std::thread); PyInstaller onefile needs parent-pid watchdog; hardenedRuntime=false for ad-hoc-signed sidecars; WKWebView AX degrades after relaunches.

## Watchouts

- Local main must be PUSHED before spawning workers (worktrees come from origin/main).
- Agents rewrite todo.md/map.md/hot.md concurrently — REFETCH before writing (collision happened 07-08).
- Pipeline exits lie: `aws|sed>file` exits 0 on aws failure — check file content.
- NEVER `pkill -f` prompt substrings; two-dot diffs show fake "reverts"; JSONL parsers drift.
