---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-09-17
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **Splitty (09-17 ~15:05 EDT): now a repo + 2-worker fleet (orch splitty).** Lew Universal beat official but still dampens dense vocal overlaps; max-spec of two inst_v2-based chains can't fix shared-separator failure bins. Repo github.com/hwang2409/splitty (private) created: seeded `manifest.json` (11 candidates w/ pipelines), `scripts/maxspec.py`, apollo patch file; `out/`+`models/` gitignored. Lanes: SPLIT-1 (cdx luna) = more community Apollo fine-tunes + separator-diversity ensemble (Lew-restored BS-RoFormer stem vs inst_v2 chains — the anti-overlap lever); SPLIT-2 (cc opus-4.7) = "Splitty Lab" web app (manifest catalog, inline playback w/ Range, A/B deck w/ playhead-preserving switch, port 8321). Fleet monitor armed (b28319tzz). Merge auth for splitty repo NOT yet granted — surface clean passes. Then Henry's ears; Tier 2 if overlaps still dampen. Details [[splitty]].

- **Pausanias: PAUS-14 LOCOMO retrieval-only benchmark MERGED (pause orch, 09-17 ~17:18Z; fleet EMPTY, main b6a19dd, 15 PRs).** Key-free LOCOMO run (Henry chose retrieval-only over Ollama/Claude-judge options): fused 55.5% recall@200 vs lexical 0.081% on 1,540 questions, zero fallbacks, fused p95 14.5 ms; NOT mem0-judge-comparable (scope note in eval/PAUS-14-RESULTS.md). 4 review rounds peeled honest-diagnostics defects (missing->empty->partial diag dicts as false zeros; predict+evaluate flag-combo bypass now rejected). Judge-scored LOCOMO still BLOCKED on OPENAI_API_KEY (ANTHROPIC_API_KEY in orch env is EMPTY). Next arc: agent integration (Claude Code hook adapter). Watchout: reused-worktree venvs can symlink deleted sibling worktree interpreters — gate runs build fresh venvs. Details [[pausanias]].

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
