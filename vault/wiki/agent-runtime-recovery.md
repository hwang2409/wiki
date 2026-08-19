---
type: reference
tags: [wiki, ops, runbook, agent-runtime, sqlite, recovery]
created: 2026-08-17
updated: 2026-08-17
---

# Wiki agent-runtime recovery runbook

Runbook for the `~/.wiki/agent-runtime/` state directory that backs Wiki.app's orchestrators, workers, event store, and supervisor. Written after the 2026-08-17 outage that started as SQLite corruption and cascaded into an unrecoverable supervisor boot loop.

## The 2026-08-17 incident (RCA)

**Chain, start to finish:**

1. **~11:35** — the wiki supervisor woke up, ran `_rebuild_materializer_database` (`backend/app/agent_runtime/supervisor.py:3113`) because it detected orphan events or a `materialized_seqs` mismatch in `events.sqlite3`. The rebuild wrote a fresh 396 MB copy into `events-rebuild-jwc6s1ug/` and was about to `os.replace()` it in.
2. **~11:35** — someone (Henry, from a prior session) intervened mid-flight and manually renamed the tempdir `events-rebuild-jwc6s1ug` → `events-rebuild-jwc6s1ug.bak-2026-08-17-1135/`, preserving the completed-but-not-swapped rebuild. Also moved `runs/` aside to `runs.bak-2026-08-17-1135/` (491 run dirs, ~16.6 GB).
3. **~11:40** — a bulk manual rotation gave `.bak-2026-08-17-1140` suffixes to every other file in `~/.wiki/agent-runtime/` (`command-log.sqlite3`, `agent-viewed.json`, `cost-aggregation.json`, `autopilot/`, `events.sqlite3`, etc.). The `.bak-…-1140` suffix isn't in any wiki source — pure manual/scripted rotation.
4. **Post-11:40** — `events.sqlite3` was reset to 4 KB (essentially empty). Workers began writing new events. Over 2h15m it grew 4 KB → 905 KB.
5. **~13:45** — SQLite reported `database disk image is malformed` on Tree 18 (dozens of `btreeInitPage() returns error code 11`). Every worker's provider failed with `provider event persistence failed: database disk image is malformed`.

**Proximate cause of the SQLite corruption (leading theory, not proven):**

Two Wiki.app binaries were in play simultaneously. macOS Launch Services was resolving `open -a Wiki` to a **stale bundle at `~/me/fun/wiki/.codex/worktrees/wiki-43-terminal-fidelity/src-tauri/target/release/bundle/macos/Wiki.app`** rather than the main-tree build. That stale binary predated recent event-store changes (missing `/api/models`, older schema assumptions). If both binaries were ever alive at the same time during the manual reset window — each opening `events.sqlite3` with its own connection pool, different PyInstaller-bundled sqlite versions, potentially different journal-mode expectations — divergent writers will corrupt a btree page. This matches the symptom (page corruption on Tree 18, consistent for every subsequent write).

**Recovery cascade (what actually took hours):**

- Restoring the pre-rebuild 396 MB `events.sqlite3` alone was not enough. State was inconsistent: DB referenced 30 runs, but `runs/` only had 21 (the 11:35 manual move parked 470 others). Recovery detected the mismatch → triggered `_rebuild_materializer_database` on every boot → rebuild spun in a giant `json.dumps` inside `SQLiteEventStore.materialize`, tempdir stopped growing at ~54 MB / 14 % → supervisor got killed by whatever timeout applies → next boot picked the same DB and started fresh again → new tempdir every ~60 s.
- Symptom: supervisor process pegs one CPU at 90-97 %, **never creates the socket** at `~/.wiki/agent-runtime/supervisor.sock`, and every backend endpoint that calls `_supervisor_request` returns 503 `"Agent supervisor is unavailable"`.
- The fix that broke the loop: full fresh start — move `events.sqlite3` and `runs/` aside, park `autopilot/` too (it holds per-ticket locks that reference dead runs), let the supervisor boot with zero runs. Socket appeared in 9 s. Lost worker history; PRs and tickets are in Linear/GitHub and re-spawnable.

## Recovery recipe (tl;dr)

Symptom: UI says "agent supervisor is unavailable" for every control action. Backend returns 503 on POST endpoints. `~/.wiki/agent-runtime/supervisor.sock` doesn't exist. `pgrep -fla "wiki-backend --supervisor"` shows a process pegged near 100 % CPU. New `events-rebuild-*` directories appear every ~30-60 s.

```bash
# 1. Kill everything cleanly.
osascript -e 'tell application "Wiki" to quit'
sleep 2
pkill -9 -f "wiki-backend --supervisor"
pkill -9 -f "wiki-backend --host"
pkill -f "wiki-artifacts-mcp"
pkill -f "codex app-server.*WIKI_AGENT_ID"

# 2. Confirm nothing holds the runtime dir open.
lsof /Users/henry/.wiki/agent-runtime/events.sqlite3  # should be empty

# 3. Fresh start (preserves everything as .parked-<ts> for postmortem).
TS=$(date +%Y%m%d-%H%M%S)
cd /Users/henry/.wiki/agent-runtime
mv runs runs.parked-${TS}
mv events.sqlite3 events.sqlite3.parked-${TS}
mv autopilot autopilot.parked-${TS}
rm -f events.sqlite3-wal events.sqlite3-shm
# also drop any incomplete rebuild tempdirs (leave the timestamped .bak-* ones)
for d in events-rebuild-*; do case "$d" in *.bak-*) ;; *) rm -rf "$d" ;; esac; done

# 4. Launch by explicit path — never by name (Launch Services will pick a stale worktree bundle).
open /Users/henry/me/fun/wiki/src-tauri/target/release/bundle/macos/Wiki.app

# 5. Verify.
for i in $(seq 1 60); do
  [ -S /Users/henry/.wiki/agent-runtime/supervisor.sock ] && echo "socket up after ${i}s" && break
  sleep 1
done
curl -s http://127.0.0.1:8213/api/agents | python3 -c "import json,sys;d=json.load(sys.stdin);print(d.get('supervisor'))"
```

The stale registry keeps old worker rows referencing dead PIDs. Archive them from the UI or via `POST /api/agents/{ticket}/archive` after the fresh boot, then re-spawn the phoebe/wiki/tooling orchestrators.

## Less-destructive alternatives (when to reach for them)

Try in this order — most-preserving first:

1. **Just corruption, everything else clean** — swap the corrupt `events.sqlite3` for the healthy 396 MB pre-rebuild backup if it exists (`events.sqlite3.bak-…` or `events-rebuild-*.bak-…/events.sqlite3`). Only works if `runs/` and DB are still consistent. Recipe: kill everything (step 1 above), `mv events.sqlite3 events.sqlite3.corrupt-<ts>`, `cp <backup> events.sqlite3`, `rm events.sqlite3-{wal,shm}`, relaunch. Verify with `sqlite3 events.sqlite3 "PRAGMA integrity_check"`.
2. **DB↔runs mismatch, tolerable loss window** — restore the parked `runs.bak-…/` back into `runs/` so the DB and disk match again. Loses any run data written after the parking timestamp. Runtime dir will be big (~16 GB in the 2026-08-17 case).
3. **Surgical prune** — delete rows in `events.sqlite3` for runs whose directory no longer exists. Preserves live-run state exactly, loses the parked ones. Requires SQL and cross-checking the rebuild triggers (`not database_healthy` / `recovered_any` / `materialized_seqs mismatch` — see `supervisor.py:3113`). Slowest to execute; only worth it if the live runs are individually irreplaceable.
4. **Fresh start** (the runbook above) — last resort but the reliable escape hatch from boot loops.

## Diagnosis cheat sheet

- `agent supervisor is unavailable` in the UI → check `ls /Users/henry/.wiki/agent-runtime/supervisor.sock`. Missing = supervisor stuck at startup, hasn't reached `server.start()` in `backend/app/agent_runtime/daemon.py:169`.
- Supervisor process at ~100 % CPU with no socket → it's stuck inside `supervisor.recover_on_start()`, almost certainly `_rebuild_materializer_database`. Look for a growing `events-rebuild-*/events.sqlite3` in the runtime dir.
- Every worker `runtime_state: "blocked"` with `state_reason: "provider event persistence failed: database disk image is malformed"` → SQLite corruption. `sqlite3 events.sqlite3 "PRAGMA integrity_check"` to confirm.
- Supervisor logs a repeating `RuntimeError: wiki supervisor is already running` after a `BlockingIOError: [Errno 35]` → normal noise from the host's retry loop losing the flock race, not a real error.
- `WorkgraphError: unsafe workgraph ticket` for tickets like `PHO-RCA-019FFCE5` or `SKILLS-DESLOP` in `supervisor.log` → the regex `^[A-Z][A-Z0-9]+-[0-9]+(?:-[A-Z0-9]+)*$` in `backend/app/workgraph.py:84` rejects hex suffixes and letter-only suffixes. Non-fatal noise, but a signal that some archive-reconcile edges will silently drop.

## Preventive hygiene

- **Never `open -a Wiki`** — always launch by explicit bundle path: `open /Users/henry/me/fun/wiki/src-tauri/target/release/bundle/macos/Wiki.app`. Launch Services caches bundle locations across all `.app` copies it has ever seen, including under `.codex/worktrees/*/src-tauri/target/release/bundle/macos/Wiki.app` and `.native-build-staging/*/`. Any of those can win the race and boot a stale binary against your live runtime dir.
- Periodically prune stale bundles: `find ~/me/fun/wiki -type d -name Wiki.app` and delete anything outside `src-tauri/target/release/bundle/macos/` that you don't need.
- After every `make native-build`, sanity-check the running binary: `curl -s http://127.0.0.1:8213/openapi.json | python3 -c "import json,sys;print(len(json.load(sys.stdin)['paths']))"` — if the count looks small compared to `git grep -c '@app\.\(get\|post\|put\|delete\)' backend/app/main.py`, you're on a stale bundle.
- Don't rotate `runs/` and `events.sqlite3` independently — they encode the same durable state and the supervisor's recovery loop enforces them being in sync. If you're going to reset one, reset the other in the same transaction, and clear `autopilot/` too.
- If you must intervene in a live `events-rebuild-*` tempdir, know that the wiki app has moved its `.swap-complete` sentinel — don't leave partial rebuilds around; they'll clutter without effect.

## Related

- Runtime paths: `backend/app/agent_runtime/store.py:487` maps env → filesystem layout.
- Supervisor lifecycle: `backend/app/agent_runtime/daemon.py:144` (`run_daemon`).
- Rebuild trigger: `backend/app/agent_runtime/supervisor.py:3113` (call site) and `supervisor.py:3280` (`_rebuild_materializer_database`).
- Frontend error text: `backend/app/main.py:5435` (`_supervisor_request` translates `SupervisorUnavailable` into HTTP 503 with the "Agent supervisor is unavailable" detail).
- [[orchestrator-worker-protocol]] — orchestrator-worker doctrine that governs re-spawn after a reset.
