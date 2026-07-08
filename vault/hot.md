---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-08
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki polish fleet DONE 2026-07-08 — ten PRs merged** under wiki-dev orchestrator: #1 native Tauri app, #2 review surface, #3 transcript/CSS polish, #4 spawn workers from UI, #5 twelve themes, #6 spawn orchestrators, #7 C-a leader keys, #8 keyboard focus model (pane scope), #9 unified session surface, #10 Conductor-inspired restyle (styling-only, 12-theme safe). [[wiki-app-ui-direction]], protocol [[orchestrator-worker-protocol]].
- **WIKI-12 IN FLIGHT** (cdx worker @426, gpt-5.4 xhigh, branch wiki-12-windows): tmux window model redesign — window = persistent split layout (localStorage, versioned, migrate current layout → window 0), panes = runs+notes, single-home invariant for runs, displaced runs solo out; C-a j/k pane-cycle, h/l window prev/next, 0-9 jump, w sole chooser w/ move semantics; REMOVES C-a s, n/p, (/); status bar shows windows not agents. Signed-off spec = /tmp/wiki-12-spec.md (1:1 contract, plan-dispute hatch).
- **PR #10 reviewer follow-ups (minor, unowned)**: dead `.session-send:hover:not(:disabled)` rule (shadowed — delete); spawn modal needs max-height+overflow-y (clips at ~757px); gruvbox-light/solarized-light theme blocks missing --shadow-* overrides; spawn-select chevron hardcodes `%23888`.
- **phoebe arc 07-07/08: NINE merges** — 13142 charts, 13160+13165 Slack linking, 13146 snippets, 13153 read/slice/diff, 13172 Twilio errors, 13074 taxonomy+alerting, 13144 WellSky writeback. Follow-up PHO-13207. **PHO-13206 tool audit DELIVERED** — P0s awaiting Henry. **PHO-13157 (#10751)** held for team-lead review; **PHO-13138 (#10716)** terraform-parked. See [[admin-agent]].

## Recent facts

- Orchestration loop proven 11×: push main → spawn (prompt file, status protocol, sentinels, fixture-cleanup enumeration) → monitor status file → steer → independent review subagent gate → merge → archive-then-done → rebase-steer siblings.
- Gate rule (post PR #9 flex bug): verification screenshots must be LOCAL /tmp paths; reviewer must open them from disk. GitHub-attachment-only evidence fails.
- PR #9 lesson: missing `flex-direction: column` on flex column container → zero-width transcript + 3.6M px height via overflow-wrap:anywhere. Check width chains when layout "messed up".
- check-aliveness skill revives stalled workers; false positive: panes QUOTING error strings — spinner beats signature grep.
- Prod tunnel: `aws sso login --profile phoebe-production-admin`; SSM needs AWS_PROFILE+region; nohup from foreground for fresh SSO creds.

## Watchouts

- Local main PUSHED before spawning workers (worktrees from origin/main).
- Agents rewrite todo.md/map.md/hot.md concurrently — REFETCH before writing.
- WIKI-11 pane-scope keys (plain j/k/u/d, Esc chain, i) must survive WIKI-12 — only PREFIXED C-a chords change.
- Pipeline exits lie; NEVER `pkill -f` prompt substrings; JSONL parsers drift; two-dot diffs fake "reverts".
