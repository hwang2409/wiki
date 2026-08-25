---
type: til
tags: [wiki-app, supervisor, incident]
created: 2026-08-24
updated: 2026-08-24
---

# Archive stall recurrence 2026-08-24 evening

The "archive is already in progress" stall recurred ~5.5h after the second fresh-slate wipe, on a fully fresh runtime with WIKI-358 (writer executor, PR #289) in the running binary. This proves it is a live server-side bug, not stale-state corruption.

## Timeline (2026-08-24, UTC)

- 21:58:09 — last successful command receipt (`run/archive` NEWT-62-REVIEW7, ok).
- 21:58:33 — `run/archive arch-n52pr6-2026-08-24` (NEWT-52-PR6) admitted; never receipted. Head-of-line for everything after.
- 22:00–22:13 — 17 intents pile up with no receipts: spawns NEWT-62-PR7, NEWT-52-REVIEW9, ARMDEMO-1-REVIEW1, PHO-16593-PR7, PHO-16437-REVIEW4; archives for PHO-16593-REVIEW3 (x2); steers to phoebe/zeta/tooling.
- Clients time out (15s HTTP budget) → `BrokenPipeError: supervisor client task failed` flood in supervisor.log.

## Evidence

- `command-log.sqlite3`: 17 pending `command_intents`, zero receipts after 21:58:09. Intents are durable; none executed.
- supervisor.log: `backend.app.agent_runtime.store.StoreConflict: archive is already in progress`, plus tasks parked in `asyncio/locks.py wait` via `workgraph_service.py:364 record_archive_sync`.
- Supervisor pid 2259: ~100% CPU, 26min CPU time over 38min uptime. `sample` shows top-of-stack dominated by `__wait4` — busy-waiting on a child that no longer exists (the NEWT-52-PR6 provider process was already gone while its archive stayed "in progress").
- NEWT-52-PR6 run dir (56M) still present in `runs/`; archive never materialized.

## Root cause (CORRECTED after stall #2, proven by py-spy at 22:38Z)

The initial wait4-spin/reaper theory was WRONG — `sample`'s `__wait4`/`__psynch_cvwait` top-of-stack counts were idle child-watcher and worker threads (blocked threads dominate `sample` output regardless of CPU use). The py-spy thread dump of stall #2 (supervisor 68131, saved at `/tmp/py-spy`) shows the real chain:

1. A run completes; `backfill_headless_runs` (WIKI-282 parity harness, background executor thread) selects it, takes the per-run event-store lock (`archive_parity.py:645`), then runs `compare_run_boundaries` -> `_compare_raw_prefixes`: a FULL raw replay per raw-event prefix — O(events^2) materializations with per-event sqlite commits. Hours for busy runs. This is also the mystery ~100% steady-state supervisor CPU.
2. The orchestrator archives that same run seconds later (tooling's complete->archive cadence). `_archive_finalize_sync` -> `_rebuild_materializer_database_sync` -> `replace_run_from` blocks on the same run lock (`event_store_shard.py:2019`) for the whole sweep.
3. `_archive_admission` set `supervisor.archive_inflight` BEFORE the executor hop, so while the archive waits: every command touching that agent raises StoreConflict "archive is already in progress" (`supervisor.py:1199-1209`), and the single global FIFO command reactor ([[orchestrator-worker-protocol]]) wedges behind the stuck archive — total fleet write outage, BrokenPipe flood, 1s reaper StoreConflict spam.

Trigger = archive lands while the parity backfill holds that run's lock. Explains both incidents, the fast recurrence (backfill restarts each boot with everything unbackfilled — fresh runtime made it MORE likely, not less), and the load correlation. Root causes 2 (global FIFO) and 5 (stacked timeouts) from the spec are what escalate one stuck command into a fleet outage — WIKI-376 remains the structural cure.

## Fix (implemented same evening, WIKI-377)

- `archive_parity.py backfill_headless_runs`: release the run lock after replay+swap, BEFORE `compare_run_boundaries`; a compare racing an archive degrades to the existing harness_error path and the cursor advances.
- `event_store_shard.py replace_run_from`: bounded lock acquire (15s default) raising retryable `StoreConflict` — any future long hold fails one command instead of wedging the fleet.
- Regression tests: `test_archive_parity.py::test_backfill_releases_run_lock_before_boundary_compare` and `::test_replace_run_from_fails_fast_when_run_lock_is_contended`. Full archive/event-store/supervisor suites green.

## Recovery (executed 22:16-22:33Z, successful)

Procedure that worked ("option 2"):

1. Back up `command-log.sqlite3`.
2. SIGKILL the stuck supervisor (SIGTERM's 15s grace never suffices under load).
3. While it is down, `DELETE FROM command_intents` for the poisoned request_id (`arch-n52pr6-2026-08-24`) so boot replay does not re-trigger the stall.
4. The backend daemon auto-respawns the supervisor — no manual start needed.
5. Kill orphaned provider processes (claude orchestrators survive with dead stdio, ppid 1; codex app-servers exit on their own).

Outcomes:

- Boot recovery ran ~17 min at 100% CPU before intent replay started — sequential per-run projection rebuild over 383MB of event logs (S3/S4 O(history) cost, boot form). Reads stayed fast throughout (#288 works).
- All 16 intents then applied: 5 spawns materialized, steers receipted, archives completed. Steers to dead runs receipted ok (durable delivery).
- A fresh NEWT-52-PR6 archive attempt succeeded post-restart once that run's events rebuild finished; the two duplicate retry intents resolved `RunNotFound` — correct and harmless.
- Total command-write outage: 21:58-22:26Z (~28 min).

## Follow-ups

- Bound or amortize the `_compare_raw_prefixes` O(events^2) sweep — no longer outage-causing, but still burns ~100% CPU for hours per large run (noted under WIKI-377's ticket line).
- `unknown-kind-telemetry.json` carries 338 legacy run cursors + old ticket names (PHOEBE-*, TPUF-RESEARCH); it was rewritten at 17:26 local, post-wipe, so something rehydrates it. Boot reconciliation re-fails `WorkgraphError: unsafe workgraph ticket` on every legacy ticket, every boot — wasted work + log spam.
- Two run dirs both claim agent_id PHO-16929-PR2 — pre-existing duplicate, untriaged.
