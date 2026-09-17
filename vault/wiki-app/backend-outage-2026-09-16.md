---
type: reference
tags: [wiki-app, outage, rca]
created: 2026-09-16
updated: 2026-09-16
---

# Backend outage 2026-09-16

Instance of the recurring shared-backend P1 (wiki orch owns root cause). Henry saw all runs vanish from the UI ~11:52-11:55 EDT.

## Timeline (EDT)

- ~10:53 — Wiki.app relaunch after the second full reset; backend log `wiki-backend-1789570375.log` starts.
- 11:51:xx — backend receives graceful shutdown ("Shutting down / Waiting for connections to close"), then `terminated signal=9`. Sender unidentified — TERM-then-KILL escalation pattern matches the documented sidecar-kill recovery, not the watchdog (watchdog logged "backend exited unexpectedly; attempting respawn 1/3").
- 11:52 — wiki-native watchdog respawns backend (pid 20276) after 250ms. `/health` 200 immediately.
- 11:52-11:55 — respawned instance saturated: `/health` fast, but `/api/agents` (plain AND `include_history`) hung >20s; UI polls stopped completing at 11:54:53, so the runs list emptied. Backend at ~54% CPU.
- 11:55:50 — self-recovered; `/api/agents` back to 200 in ~4s (still slow), 3 orchestrator runs listed, all `working`.

## Evidence

- Thread sample (pid 20276, `sample 20276 3`): most AnyIO worker threads parked in `lock_PyThread_acquire_lock`; one hot worker burning CPU in `_json.scan_once_unicode` (large JSON parse) while holding whatever lock the others wait on. Sample saved during incident; pattern = one big JSON parse serializes all threadpool endpoints.
- Candidate large-JSON inputs touched at kill time: `~/.wiki/token-cache.json` (6.1MB, mtime 11:51), `~/me/fun/agent-archive/archive-catalog.json` (220KB — backend held two open read fds on it).
- Log also spammed with `WorkgraphError: unsafe workgraph ticket` 500s from `GET /api/agents/zeta/workgraph` (backend/app/workgraph.py:128 via main.py:2851) — separate defect, constant UI-poll 500s, predates the kill.
- No run/process loss: zeta + phoebe orchestrators (etime ~58m) and ZETA-129/130 workers survived the whole window. Outage was UI-visibility only.

## Post-incident (same day, ~16:15 EDT)

Henry ordered a third full reset: all runs killed, `~/.wiki` sqlite + agent-runtime + workgraphs + `/tmp/agent-status` wiped, rebuilt from origin/main `c240d633`, relaunched. Confirmation of the wedge mechanism during the fresh boot: while `knowledge.db.rebuilding` existed (rebuild grew the db to ~500MB over minutes), `/api/agents` hung 10-15s, then dropped to ~2s once warm. The knowledge index rebuild/refresh path serializes the agents API — prime suspect for the recurring P1.

## Open questions

- Who sent the TERM+KILL at 11:51 (an agent running sidecar-kill recovery? which one, and why — was the pre-kill instance wedged the same way?).
- What exactly parses multi-MB JSON under a shared lock on the `/api/agents` path — root-cause target for the P1.
- `unsafe workgraph ticket` 500 spam for the zeta workgraph needs its own ticket.

Related: [[archive-stall-recurrence-2026-08-24]], [[supervisor-fingerprint-swap-wedge]].
