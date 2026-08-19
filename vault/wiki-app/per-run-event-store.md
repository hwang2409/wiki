---
type: til
tags: [wiki-app, event-store, reliability]
created: 2026-08-18
updated: 2026-08-18
---

# Per-run event store — kill the shared sqlite failure class

Design note. Not yet ticketed.

## The failure class

State reasons seen (all leave the run `blocked`, orchestrator must archive + respawn):

- `provider event persistence failed: [Errno 9] Bad file descriptor` — WIKI-336 2026-08-18 ~21:05Z
- `provider event persistence failed: database disk image is malformed` — cc run 2026-08-18 ~19:46Z (PRAGMA `quick_check` was ok on both `events.sqlite3` AND `command-log.sqlite3` right after — transient corruption view)
- Older wedges: multi-minute event-store rebuild at ~100% CPU with no socket after `runs/<id>/` without `run.json` crash-loops recovery.

## Current architecture (2026-08-18)

- `runs/<run_id>/raw.jsonl` — provider events, one writer per run, append-only (already Pi-style).
- `runs/<run_id>/events.jsonl` — normalized events, per-run.
- `runs/<run_id>/run.json` — durable metadata.
- **`runtime_dir/events.sqlite3`** (currently 224 MB) — SHARED materialized view: `runs`, `events`, `patches`, `run_cursors`, `dispositions`, `run_projections`, `parity_records`, `backfill_progress`, `child_runs`. Schema v8, WAL, `busy_timeout=5000`.
- `runtime_dir/command-log.sqlite3` — smaller, less contended.
- Writer: the sidecar's `_pump_events(run_id, adapter)` coroutine — one task per active adapter, all sharing the events.sqlite3 connection pool via `EventReducerAdapter`.

## Why RLIMIT_NOFILE alone isn't enough

`backend/app/nofile_limit.raise_nofile_limit()` already lifts soft to hard (or 10240 on darwin) at sidecar startup — the earlier P1 defense. WIKI-336 still died with bad-fd after that fix, so fd exhaustion isn't the whole story; shared-connection corruption under concurrent writers is the residual class.

## Henry's proposal (2026-08-18)

Per-worker append-only event log, merged on read. One writer per file → zero contention, zero shared-lock corruption. Pi reference: `vault/tools/harness-pi-coding-agent.md` (JSONL append-only, crash-resistant, no transaction overhead).

## Fit against current wiki

Raw path already IS this pattern. The materialized view is what still shares. Two shapes:

**A. Per-run SQLite (recommended).** Move `events.sqlite3` to `runs/<run_id>/events.sqlite3`. Keep schema v8 unchanged. One writer per file. Blast radius: one worker.

- Pros: preserves the whole SQL read surface (event pagination, `run_cursors`, `run_projections` queries); rebuild scope shrinks 224 MB → few MB per run; dead worker's fd damage stays local; archive = rename directory; migration is a one-shot script that shards rows out of the shared file.
- Cons: cross-run tables (`parity_records`, `backfill_progress`, `child_runs`) either move to a small shared `runtime_dir/metadata.sqlite3` (low write rate, low blast radius) or go per-run (child_runs = per-parent, parity = per-run). Fleet-wide reads iterate per-run DBs — already the pattern for `list_agents` reading per-run `run.json`.

**B. Pure JSONL per run.** Drop sqlite from the runtime read path entirely. Derive the projection on read from `events.jsonl`. Add an in-memory index cache with mtime invalidation.

- Pros: matches Pi verbatim; zero DB machinery per run; fewer files (no `-wal`, no `-shm`).
- Cons: read-path rewrite is large — event pagination, disposition counts, projection lookups, all currently SQL. In-memory materialization is fine for one-run views but a hot fleet list would re-scan every run's JSONL each request without a cache layer. Bigger change for the same failure-class win.

## Recommendation

Ship **A** first. Same schema, sharded writer, contained blast radius — that removes the class Henry raised. Revisit **B** only if the read code path is later simplified for other reasons.

## Migration sketch

1. Add `runtime_event_db_path(runtime_dir, run_id)` → `runtime_dir/runs/<run_id>/events.sqlite3`. Keep the old shared path only for the migration reader.
2. On sidecar start, if the shared `events.sqlite3` exists and per-run DBs are missing: iterate rows by `run_id`, write into each per-run DB, then rename the shared file to `events.sqlite3.legacy-YYYYMMDD`.
3. Move `parity_records`, `backfill_progress`, `child_runs` into `runtime_dir/metadata.sqlite3` (rare writes, no per-worker contention).
4. `EventReducerAdapter` opens its per-run connection lazily; supervisor `_pump_events` no longer serialises across runs — each run's write path is independent.
5. Update `archive_parity` and any cross-run reader to iterate per-run DBs (already partially the pattern via `runs/<id>/` scans).

## Test surface

- `backend/tests/test_event_store.py` — schema + write/read cases; add a per-run isolation case (kill one run's DB, prove others keep serving).
- `backend/tests/test_agent_runtime_supervisor.py` — the existing `provider event persistence failed: fixture fsync failed` test guards the pipeline-failure path; add a case where one run's fsync fails and others keep pumping.
- New: fault-injection test that opens a per-run DB, invalidates its fd, and asserts (a) that run goes blocked, (b) other runs continue, (c) the raw.jsonl is intact and a fresh materialize on respawn recovers.

## Related

- [[orchestrator-worker-protocol]] — archive-then-respawn playbook for this failure class.
- [[harness-pi-coding-agent]] — reference for the append-only pattern Henry pointed at.
- `hot.md` watchout — transient event-store write failure (2026-08-18 ~19:46Z).
- todo P1: RLIMIT_NOFILE already landed (`backend/app/nofile_limit.py`) — necessary but not sufficient.
