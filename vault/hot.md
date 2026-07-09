---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-09
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki: 24 PRs merged lifetime; perf arc underway.** 07-08/09 wave: #20 pane-remount fix (stable WorkspacePane, focus=prop), #21 focus-styling parity, #22 open semantics (aux panel scoped to pane, click=new window, spatial j/k), #23 external links→system browser, #24 `agent done` auto-kills windows, #25 C-a leader overrides composer, #26 cold paths (async tokens+refreshing flag, ONEDIR sidecar 4.6s→0.4s, graph RAF pause), #27 transcript VIRTUALIZATION (DOM 19,966→850, WebKit open 1.29→0.51s, typing 4.3→0.99s). [[wiki-app-ui-direction]]; perf audit in agent-archive/WIKI-30.
- **WIKI-32 IN FLIGHT** (cdx @495 xhigh, wiki-32-polling): central transcript store, visible-only polling, scoped SSE, patch-by-id deltas (500KB→<30KB/tick), fixes WIKI-18 landing-poll bug. Wiki queue: WIKI-24 window-switch state preservation (P1), WIKI-27 header jump, WIKI-34 watchdog same-day reset parse, WIKI-35 session-view status banner, WIKI-36 backend registry env override (P1).
- **Watchdog fired twice 07-08** (primary→secondary→tertiary; all 3 burned; Henry reset tertiary). v1.1 revival preserved sessions. Gap left: WIKI-34 (bare "8:01 PM" reset format unparsed → thrash risk).
- **phoebe arc — THREE MERGES 2026-07-09 AM** (new orchestrator, old session ac7e9e74 died — handoff: `~/me/fun/agent-archive/orchestrator-ac7e9e74/handoff.md`; keep security briefs factual/short): PHO-13251 (#10887 cross-org feature-adoption tool), PHO-13227 (#10861 subagent approval hole, 3 gate cycles), PHO-13216 (#10856 Core dossier/funnel T2) all merged + wrapped after review-gate steer cycles. Remaining fleet: PR-10475 parity repair reruns (~4/11); PHO-13157 (#10751) team-lead-held; PHO-13138 (#10716) terraform-parked. Unspawned queue: PHO-11275, PHO-11267, 13172 golden-case batch, pandadoc guard port, PHO-13207 calls. See [[admin-agent]].

## Recent facts

- `codex resume <id>` REUSES the rollout file (open-file-handle verified) — resolver: exact registry sid beats same-cwd chain (chain cross-bled tickets sharing repo-root cwd). ALWAYS `wiki agent update --session <id>` after revival; true sid via `lsof` on the pane PID when in doubt.
- Backend hardcodes /tmp/agent-registry.json — "isolated" backends still hold LIVE send-keys powers; a review agent typed probes into the orchestrator composer (07-09). Codified in both tmux-ticket skills: agent-route verification = patched module constants + fake @9999 windows. WIKI-36 = proper env overrides.
- Kickoff prompts MUST say `ticket <ID>`; cwd-mismatch resume chooser → answer "2"; claude spawns can leave prompt unsubmitted (verify + Enter).
- Subagents stall (600s watchdog) on network drops — relaunch, don't debug prompts.
- Workers never commit vault/ into ticket branches.

## Watchouts

- Wiki NATIVE app = FROZEN build: backend/frontend fixes reach Wiki.app only after `make native-build` + relaunch (curl-on-8011-verified ≠ user-visible; bit us on PR-10475).
- `wiki agent done` AUTO-KILLS the worker window now (`--keep-window` opt-out); todo complete needs a UNIQUE substring (WIKI-31/32 collision).
- Rotation kills ALL registry cdx workers cross-orchestrator — phoebe workers bounce when wiki's watchdog rotates.
- Local main PUSHED before spawns; refetch todo/map/hot before writing; screenshots = LOCAL /tmp paths; empirical re-verify after rebases over rewritten files.
