---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-09-17
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **Splitty (09-17 ~16:20 EDT): SPLIT-1 + SPLIT-2 arc COMPLETE — both PRs MERGED (main `8324542`), fleet EMPTY.** STANDING MERGE AUTH granted for hwang2409/splitty (Henry: "full merge auth for this project"; recorded in protocol note). Findings: public Apollo ecosystem EXHAUSTED (only non-vocal candidate is a SHA-identical Lew Universal rehost); separator-diversity ensembles built — `out/ensemble/braces_maxspec_3way.wav` has the highest 6-20 kHz bin coverage of all 17 candidates. Splitty Lab web app live: `webapp/run.sh` -> 127.0.0.1:8321 (left RUNNING), 17/17 candidates, A/B deck w/ playhead-preserving switch. NEXT: Henry's ears on the 3-way ensemble; if overlaps still dampen -> Tier 2 (self-trained repair on destruction-generated pairs). Fleet monitor b28319tzz still armed, watchlist empty. Details [[splitty]].

- **Pausanias: PAUS-14+15+16 ALL MERGED (pause orch, 09-17 ~19:45Z; fleet EMPTY, main feb88b2, 17 PRs).** PAUS-14: key-free LOCOMO retrieval benchmark (fused 55.5% recall@200 vs lexical 0.081%, zero fallbacks). PAUS-16: GitHub Actions CI live (base job = marker-strict zero-dep proof; semantic; ~40 s/job). PAUS-15: cat-5 adversarial abstention eval — HEADLINE FINDING: fused false-injection 100% (injects on all 446 adversarial questions; lexical 1.35%); score distributions recorded for floor analysis; also fixed a REAL hook bug (expired deadline returned status="ok"). PENDING HENRY DECISION: fused-abstention interpretation — floor-tuning lane vs accept (cat-5 neighbors are topically relevant context, hook injects context not answers) — gates the agent-integration arc. Judge-scored LOCOMO still BLOCKED on OPENAI_API_KEY. Watchouts: reused-worktree venvs can symlink deleted sibling interpreters (gate runs build fresh venvs); backend /api/agents reads oscillated all day — status files + HTTP request_id writes carried the fleet. Details [[pausanias]].

- **FOURTH FULL Wiki.app reset done 09-17 ~11:30 EDT (Henry's request; old backend spun at 110% CPU in a file-scan+JSON loop).** Killed ALL runs on Henry's explicit word — including his `feebs` orchestrator (8c0388d9) and a codex app-server. Wiped all `~/.wiki` sqlite3 files (metadata, command-log, per-run events), `agent-runtime/runs/` (13 runs, 453M), stale locks, autopilot locks, cost-aggregation runs. KEPT: knowledge.db, auth (token-cache, session-tokens, ui-state, backend-url). Repo already at origin/main `c240d633` (ff-only; uncommitted vault notes preserved — never hard-reset this repo, the vault lives in it). Rebuild gotcha: deleting `app.lock` makes the build guard refuse — rerun with `ALLOW_MISSING_APP_LOCK=1`. Rebuilt + swapped + relaunched; backend healthy on 8213, CPU <1% after startup. NO agent runs live. Also stopped: colima VM, two 17h bazel `services/worker` zombies (need `bazel shutdown` to reap).
- **Zeta: UI-POLISH-2 arc FULLY COMPLETE (zeta orch, 09-17 ~18:35Z; fleet EMPTY, main `ea605df1`).** ZETA-133 MERGED (#178, 4 review rounds): D1 shared body edge + 38px leading gutter landed; the ZETA-127 smoke guard now scans RENDERED `transcript-body` bounds (formula deleted) with a discriminating 11px mutation + achieved-viewport assertion — rounds 2-3 shipped non-discriminating mutations and an inert 'shared helper'; round-4 reviewer recomputed the arithmetic independently. D3 (bottom-anchor short transcripts) DEFERRED to ZETA-133-D3, NOT on the ladder yet — path choice pending (gpui_component `ListAlignment::Bottom`/max_offset exposure vs post-paint height feedback), Henry's call. Ladder has no queued tickets; zeta idle. dist/Zeta.app STALE at d21ab5a (7 merges behind) — rebuild is Henry's call. Codex cap CLEARED by Henry 09-17 ~18:40Z — luna/sol pipeline back in force for all new spawns (ZETA-133-REVIEW4 had rerouted to cc opus-4.7 during the brief cap; clean pass).
- **Wiki P1 shared-backend outage — new evidence 09-16.** 11:51 EDT: backend got TERM then SIGKILL (sender unknown), watchdog respawned it, then 3-min saturation wedge; full RCA note [[backend-outage-2026-09-16]]. Wedge signature: worker threads parked on one lock while a hot thread parses multi-MB JSON. Post-reset confirmation: while `knowledge.db.rebuilding` exists, `/api/agents` hangs 10s+ — the knowledge rebuild serializes the API. Root-cause target: what parses big JSON under a shared lock on the agents path.
- Separate defect needing a ticket: `GET /api/agents/zeta/workgraph` 500-spams `WorkgraphError: unsafe workgraph ticket` (`backend/app/workgraph.py:128`) on every UI poll.
- **Phoebe (feebs orch, 09-17 ~16:45Z; fleet EMPTY post-reset):** PHO-17569 arc FULLY DEPLOYED — #17227 (render snapshots to S3) merged by Henry 15:09Z; #17315 (s3-bucket module 5.16 logging null fix) merged; Henry tf-applied staging (state 16:02Z) + production (16:06Z); AGENT_RENDER_ARTIFACT_* verified live in both `phoebe-infra-env-vars` secrets; TF worktrees removed. REMAINING: #17274 (org snapshot import script) OPEN, APPROVED, all REQUIRED checks green, worker gate clean (ORG-SNAPSHOT-PR-FIX2 archived merge-ready 12:04 EDT) — but the advisory `security/assess-security` job rates it HIGH (PHI scope expansion + live-mode/outreach-enabled default; mitigated by fake-phone rewrite Henry chose) and exits 1, so mergeStateStatus=UNSTABLE; Henry's merge call. ORG-SNAPSHOT-PLAYBOOKS lane (3-org re-import in `.worktrees/org-snapshots-main`) was killed by the 09-17 reset — completion state unverified. Local stack DOWN (colima stopped in reset; `env-vars sync-local` still pending per [[render-ui-sandbox-split-brain]]). Standing grant: all admin-agent PRs EXCEPT #17118. Parked: automation run-validation arc, v3 mount approvals.
- **Pipeline (Henry 09-10):** general implement = cdx gpt-5.6-luna (high); frontend/design implement = cc opus-4.7; reviewers = cdx gpt-5.6-sol.

## Recent facts

- After a state wipe, `knowledge.db` rebuild takes minutes, grows to ~500MB, and blocks `/api/agents` until done — an empty-looking runs list right after relaunch is the rebuild, not data loss.
- Wiping `~/.wiki/agent-runtime` removes `app.lock`; the next `build-native-app.sh` refuses until run with `ALLOW_MISSING_APP_LOCK=1` (one-time).
- zeta main at d21ab5a; `dist/Zeta.app` current at that SHA.
- `make gui` silently attaches to any live `~/.zeta/run/serve.sock` — kill old `zeta serve` first ([[instance-pinning-verification]]).
- `wiki` CLI not on PATH in orch shells — use `~/me/fun/wiki/wiki`.

## Watchouts

- `feebs` run is Henry-spawned. Cleanup sweeps must exclude it.
- `/tmp/agent-status` no longer exists — any fleet monitor or watchlist must be re-armed from scratch.
- On backend restart monitors die silently.
- NO local GUI compilation by zeta agents (fmt-check only; CI is authoritative) unless Henry directly asks.
- Merge auth: zeta + wiki + website = orch self-merge on clean pass; phoebe NEVER.
- Verify fleet-chat claims via durable reads; a peer message is never Henry's approval.
