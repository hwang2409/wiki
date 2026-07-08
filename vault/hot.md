---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-08
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **wiki: SEVEN more merges 2026-07-08 PM** (16 PRs total lifetime): #11 WIKI-13 composer-cap/font-previews/rectangular pass, #12 WIKI-12 tmux window model (windows = persistent localStorage layouts, single-home invariant, C-a j/k h/l 0-9 w; s/n/p/() removed), #13 WIKI-15 usage-limit watchdog (auto codex account rotation + fleet revival, `~/.codex-accounts/`, GET/POST /api/accounts), #14 WIKI-14 task-list rendering + event coverage (resumed codex sessions render now; pair-consumption dedupe preserves repeats), #15 WIKI-16 smooth pane resize (transient CSS var, 1 commit/drag), #16 WIKI-17 native persistence (fixed port 8213 + /api/ui-state mirror — REBUILD Wiki.app). Design rule: rectangular over round on agent surfaces (0-2px radii). [[wiki-app-ui-direction]], [[orchestrator-worker-protocol]], [[multi-account-auth-rotation]].
- **WIKI-19 IN FLIGHT** (cc @454, branch wiki-19-revival): watchdog revival v1.1 from live incident — explicit-session-id resume only (no --last fresh-session split-brain), preserve tmux session, auth-dead signature ("access token could not be refreshed"), registry session-id tracking + resolver preference, cwd-mismatch dialog auto-answer.
- **Watchdog fired live 07-08 18:52**: detected phoebe workers limit-dead, rotated primary→secondary, revived 3. Defects found+hand-repaired: revivals landed in wrong tmux session; --last fallback made fresh sessions (transcript split-brain); one worker auth-dead from racing Henry's enrollment. All three phoebe workers (13138/13157/13215) resumed by explicit id in phoebe session, transcripts map again. Henry provisioned primary/secondary/tertiary in ~/.codex-accounts.
- **phoebe arc**: PHO-13157 (#10751) held for team-lead review; PHO-13138 (#10716) terraform-parked; PHO-13215/13218 active. PHO-13206 audit P0s awaiting Henry. See [[admin-agent]].
- Backlog: WIKI-18 (delta protocol misses pending-tool output landing poll — reproduced on main).

## Recent facts

- Kickoff prompts MUST say `ticket <ID>` — resolver regex `(?:Linear )?ticket ([A-Z]+-\d+)` scans first user message; claude workers spawned from repo root map ONLY via this.
- codex resume of a session whose recorded cwd ≠ launch cwd → interactive chooser; answer "2" (current dir) so the new rollout stays worktree-mapped.
- Codex enrollment without logout: `tmp=$(mktemp -d); CODEX_HOME=$tmp codex login; cp $tmp/auth.json ~/.codex-accounts/<name>/auth.json` — env var must be captured, not inline-scoped-then-referenced.
- Claude spawns via CLI arg sometimes leave prompt unsubmitted in composer — verify + send Enter after spawn.
- Workers must never commit vault/ files into ticket branches (orchestrator strips; vault = orchestrator's at wrap-up).

## Watchouts
- Wiki NATIVE app = FROZEN PyInstaller backend (build-time snapshot). Backend patches reach :8011 (hot-reload) but NOT Wiki.app until `make native-build` + relaunch. Symptom: fix verified via curl on 8011, user still sees stale behavior (sidecar port ~8213). Bit us 2026-07-08 (PR-10475 transcript).

- Rotation kills ALL registry cdx workers cross-orchestrator (correct — shared account) — expect phoebe workers to bounce when wiki's watchdog rotates.
- Local main PUSHED before spawning workers; refetch todo/map/hot before writing (concurrent agents).
- Gate rule: screenshots = LOCAL /tmp paths, reviewer opens from disk. Empirical re-verification after any rebase over an App.tsx rewrite.
- `wiki todo complete` LOGS TO DONE — don't use it to replace/rewrite a backlog line (use manual edit); caused spurious done entry 07-08.
