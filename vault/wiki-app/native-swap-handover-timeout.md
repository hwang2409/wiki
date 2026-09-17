---
type: til
tags: [wiki-app]
created: 2026-08-27
updated: 2026-08-30
---

# Native swap handover timeout

2026-08-27 rebuild+relaunch (repo at 1e2bcf31, #298): `swap-native-app.sh` failed twice with "supervisor handover failed and old bundle recovery failed", then left the runtime with NO http daemon (nothing on 8213) and a supervisor stuck at ~99% CPU.

## Root cause

- Supervisor `__init__` calls `workgraph_service.reconcile_archive_edges()` SYNCHRONOUSLY over the whole archive (`~/me/fun/agent-archive`, 2011 sessions / 15G). Startup burns minutes of CPU before the API serves.
- `native_swap_transaction.py` waits `_SUPERVISOR_STARTUP_TIMEOUT_SECONDS=15` / `_RUN_RECOVERY_TIMEOUT_SECONDS=30` for handover. Slow startup guarantees the wait times out -> rollback -> the rollback's own supervisor start times out the same way -> double failure. Deterministic while the archive stays this big.
- Side finding: reconcile logs `WorkgraphError: unsafe workgraph ticket` for every archived agent id without a numeric ticket (`ZETA-WEBTOOLS-REVIEW4`, other named zeta lanes) — `WORKGRAPH_TICKET_RE` requires `PREFIX-<digits>`. Per-entry, skipped, but adds noise + wasted work every supervisor start.

## Casualties + recovery

- The first failed swap killed the provider processes of all live runs. Handover journal (`.handover-runs.json`) snapshotted 4 idle sessions: ZETA-40, phoebe, zeta, wiki orchestrators.
- After manual swap + relaunch, the new supervisor auto-recovered ZETA-40 only. The 3 orchestrators came back as detached registry stubs (`run_id: None`) — respawn on demand; run dirs with `provider_session_id`s persist under `~/.wiki/agent-runtime/runs/`.

2026-08-30: recipe reworked cleanly (repo still at 1e2bcf31). Wiping `~/.wiki/agent-runtime` + `knowledge.db` before relaunch gave an instant-answering API (no run-recovery stall).

## Working recipe (until fixed)

1. `FORCE_STAGE_ONLY=1 ./scripts/build-native-app.sh` (builds with live supervisor; guard only blocks the swap path).
2. Kill supervisor (may need SIGKILL), GUI already closed.
3. `rm -rf <live Wiki.app>` + `mv` staged bundle in; delete stale `supervisor.pid`/`.sock`.
4. `open Wiki.app` -> GUI + daemon + supervisor start; API answers after the multi-minute archive reconcile.

## Fix candidates (file as tickets)

- Make `reconcile_archive_edges` async/deferred or incremental (it blocks supervisor init; likely also a WIKI-399 contributor).
- Size `_wait_for_handover`/startup timeouts to measured startup, or gate on supervisor readiness instead of wall clock.
- Widen or pre-filter `WORKGRAPH_TICKET_RE` for non-numeric lane ids.
