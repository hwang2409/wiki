---
type: reference
tags: [wiki-app, supervisor, perf]
created: 2026-08-19
updated: 2026-08-19
---

# Supervisor timeout diagnosis (2026-08-19)

Produced by WIKI-356 (one-shot diagnosis worker). Live profile: /tmp/wiki-356-sample.txt (ephemeral).

Date: 2026-08-19

## Findings

1. The supervisor socket and all supervisor RPCs share one asyncio event loop.
   `UnixSupervisorServer._handle_client()` awaits `Supervisor.dispatch()` at
   `backend/app/agent_runtime/protocol.py:58-89`. The daemon starts that server
   with `asyncio.run()` at `backend/app/agent_runtime/daemon.py:146-204`.

2. `ping` has no independent execution lane. It reaches the direct branch at
   `backend/app/agent_runtime/supervisor.py:5850-5862`, but the loop cannot run
   that branch while another task executes synchronous Python, SQLite, or file
   system work.

3. Provider event persistence blocks the loop. The event pump awaits an async
   wrapper, but that wrapper calls the synchronous `_dual_write_normalized()` at
   `backend/app/agent_runtime/supervisor.py:679-699`. That function performs
   JSONL fsyncs, atomic run-record writes, and SQLite materialization at
   `backend/app/agent_runtime/supervisor.py:628-677`.

4. Archive finalization also blocks the loop. `Supervisor.archive()` holds the
   per-run asyncio lock and calls synchronous `RunStore.archive_current()` at
   `backend/app/agent_runtime/supervisor.py:5069-5086` and `5144-5174`.
   `RunStore.archive_current()` holds its process lock across event export,
   file copies, fsyncs, archive commit, tree deletion, registry writes, and
   command projection writes at `backend/app/agent_runtime/store.py:1867-2012`.

5. WIKI-352 / PR #279 will reduce normal event-store overhead, but it will not
   remove the event-loop blocking. The PR keeps one locked SQLite connection,
   sets `synchronous=NORMAL`, and raises the WAL auto-checkpoint threshold.
   Its `connection()` context still runs under a synchronous lock and commits
   on the caller's thread. The PR does not change JSONL fsyncs, archive work,
   workgraph writes, or synchronous event fan-out.

6. Reads use different paths. `GET /api/agents` reads the registry snapshot,
   status files, and PID state. Its source says it intentionally never waits
   on the supervisor socket at `backend/app/main.py:2262-2269`. The CLI
   `wiki agent status` calls that endpoint at `wiki:876-894`.

7. The live sample did not catch a current 3-second ping stall. It did catch
   active SQLite and JSON work. A short sample cannot prove that no longer
   stall occurred before or after the sample.

## Profile summary

Command:

```bash
sample 55481 5 -file /tmp/wiki-356-sample.txt
```

The supervisor was PID 55481. The sample ran at 2026-08-19 14:46:52 EDT.
The process had a 341.7 MB physical footprint and a 512.7 MB peak.

The 5-second sample contained 4,333 main-thread samples:

- 3,410 samples waited in `kevent`.
- 917 samples were active in asyncio task execution.
- 320 samples were in the C JSON encoder.
- 162 samples were in Python SQLite calls.
- 146 samples were in `pysqlite_connection_execute`.
- The SQLite stacks included database-page reads, WAL shared-memory setup,
  `fcntl`, `pread`, `pwrite`, and busy-handler sleep.

The live database was about 630 MB. `events.sqlite3-wal` was 0 bytes at the
check. The live process was not at the reported 95% CPU during this sample;
the preceding process checks ranged from about 12% to 29% CPU. This is a
point-in-time profile, not a contradiction of the earlier fleet-load report.

The supervisor log also contains repeated client `ConnectionResetError` and
`BrokenPipeError` traces at `~/.wiki/agent-runtime/supervisor.log:968-1031`.
Those are consistent with callers closing sockets after their timeout.

The log contains repeated recovery scans that race with archive removal. For
example, `RunNotFound` appears at `~/.wiki/agent-runtime/supervisor.log:883-900`
while `_recovery_loop()` calls `recover_on_start()` every second at
`backend/app/agent_runtime/daemon.py:106-118`. This is extra work and noisy
failure handling under archive churn.

## Answers 1-6

### 1. What serves `supervisor.sock` ping?

The path is:

1. `SupervisorClient.request()` opens the Unix socket, sends one JSON line,
   and waits for one response. Fast reads use a 3-second timeout at
   `backend/app/agent_runtime/client.py:21-35` and `103-148`.
2. `UnixSupervisorServer._handle_client()` reads the line and awaits
   `self.supervisor.dispatch(method, params)` at
   `backend/app/agent_runtime/protocol.py:58-89`.
3. `Supervisor.dispatch()` sends non-command methods to `_dispatch()` at
   `backend/app/agent_runtime/supervisor.py:5675-5778`.
4. `_dispatch("ping")` only constructs a small dictionary at
   `backend/app/agent_runtime/supervisor.py:5850-5862`.

All of this runs on the daemon's one asyncio loop. A second asyncio task does
not help when the current task is inside synchronous code.

The synchronous sections that can exceed three seconds are:

- `RunStore.append_raw()` and `append_normalized()`: JSON serialization,
  append, fsync, atomic run-record JSON write, file fsync, and directory fsync
  at `backend/app/agent_runtime/store.py:600-608`, `684-705`, and
  `2586-2642`.
- `SQLiteEventStore.materialize()`: reducer work, multiple SQL writes, and
  transaction commit at `backend/app/agent_runtime/event_store.py:1229-1268`.
- `RunStore.archive_current()`: the full archive operation at
  `backend/app/agent_runtime/store.py:1867-2012`.
- Recovery and rebuild: orphan normalization at
  `backend/app/agent_runtime/supervisor.py:2987-3125`, and full materializer
  rebuild at `3280-3351`.
- Direct workgraph and command-log writes after archive at
  `backend/app/agent_runtime/supervisor.py:5144-5174`.

### 2. Where do archive operations run?

Archive copy and finalize run on the event loop. The async method does not
offload the store call to a thread. The copy and finalize are inside the
`RunStore._lock` critical section because `archive_current()` begins with
`with self._lock:` at `store.py:1873`.

The archive first tries SQLite export at `store.py:1933-1939`. The exporter
performs a full health check before reading dispositions at
`event_store.py:1819-1849`. It then writes a temporary JSONL file, fsyncs it,
replaces the destination, fsyncs the file, and fsyncs the parent directory at
`event_store.py:1862-1896`.

After that, archive finalization copies raw logs, normalized logs, provider
logs, prompts, status, and artifacts. It commits an archive marker, removes
the live run tree, rewrites the registry, and rewrites the command projection.

Measured isolated timings:

| case | result |
| --- | ---: |
| synthetic 1,000-event archive, 2.3 MB of copied files | 0.012 s |
| copied live run, 1,590 events, 1.3 MB raw and 1.4 MB normalized files | 2.575 s archive call |
| repeated SQLite health check on the copied 635 MB database, same 1,590-event run | 9.129 s, 6.804 s, 6.340 s |

The copied live database was not a fully consistent snapshot, so its exporter
returned `False` and archive used the JSONL fallback. That makes the 2.575 s
archive result a conservative reproduction of synchronous archive work, not a
clean healthy-export benchmark. The 6-9 second health checks show why the
SQLite validation step can exceed the 3-second ping budget. Under concurrent
event writes, lock and I/O wait can increase these times.

The small synthetic result shows that copying 1,000 small event files is not
the main risk by itself. Database validation/export, fsyncs, artifacts, and
shared-store contention determine the tail.

### 3. Why do reads stay fast while commands hang?

There are three read shapes:

- `GET /api/agents` reads the registry and status snapshots without a socket
  request. This is the path used by `wiki agent status`.
- `run/list`, `run/status`, `run/queue`, and `events/read` use the socket but
  go through direct read branches. They do not enter the durable command queue.
- `ping` also uses the socket and direct dispatch, but it still waits for the
  event loop to become runnable.

Commands enter the durable command queue. `Supervisor.dispatch()` calls
`command_queue.submit()` for command methods at
`backend/app/agent_runtime/supervisor.py:5675-5770`. The queue persists an
intent, runs the command reactor, executes the provider or archive operation,
and persists a receipt. The command reactor offloads some command-log calls,
but the command body still calls synchronous store and event-store methods.

This explains the split. Snapshot reads can finish while the command reactor
or event pump is waiting on synchronous work. A socket read can still stall if
the loop is blocked, but it has less work and no durable command completion.

Observed during this investigation:

- `GET /api/agents`: 1.346 s, 1.521 s, and 5.000 s.
- `GET /api/agents/NEWT-33/events`: 2.225 s, 0.489 s, and 0.365 s.
- `wiki agent status NEWT-33`: 2.03-3.19 s.
- Direct Unix-socket ping: 9 ms, 0.4 ms, and 0.2 ms.

The live sample window was healthy enough for ping. The earlier timeout
reports remain consistent with intermittent loop starvation rather than a
permanent socket failure.

### 4. What did the live profile show?

See the Profile summary above. The sample file is
`/tmp/wiki-356-sample.txt`.

The strongest profile evidence is not a single giant stack. Most samples were
idle in the selector, while the active slice repeatedly entered JSON encoding
and SQLite. This matches a busy event-ingestion loop with bursty synchronous
sections. The sample did not show archive symbols because no archive was
started during the read-only sample.

### 5. Will PR #279 fully fix the timeout?

It will partially fix it.

PR #279 changes `event_store.py` to keep a persistent connection per database
path, serialize access with a lock, set `synchronous=NORMAL`, and use a 4,096
page WAL auto-checkpoint. This should remove repeated connection setup and
reduce checkpoint/fsync pressure during ordinary event writes.

It does not move the work away from the event loop. `connection()` still
acquires a synchronous lock, executes the caller's SQL, and commits on the
caller's thread. It also leaves these blockers in place:

- raw and normalized JSONL fsyncs;
- atomic `run.json` projection writes and directory fsyncs;
- normalizer and reducer CPU work;
- synchronous SQLite `BEGIN IMMEDIATE`, SQL, and commit;
- JSON event fan-out and response serialization;
- archive health validation, export, copy, fsync, deletion, and registry
  writes;
- synchronous workgraph archive edges;
- synchronous command-log calls that are outside `to_thread`;
- recovery scans and full materializer rebuilds.

Therefore PR #279 should lower average CPU and event persistence latency. It
does not provide a 3-second command-path guarantee and cannot fully explain
away archive-triggered timeouts.

### 6. Ranked fixes

| rank | ticket-sized change | impact | effort |
| ---: | --- | --- | --- |
| 1 | Move archive finalize, including export, copies, fsyncs, deletion, and registry projection, to a bounded archive worker thread. Keep the event-loop side limited to state admission and completion publication. Add a test with a 1,000-event run and a concurrent ping loop. | high | medium |
| 2 | Move the synchronous event persistence bundle to a bounded writer executor. Preserve per-run order with the existing event lock, and return persistence failures to the event pump. Measure p50/p95/p99 materializer latency. | high | medium-high |
| 3 | Remove full `run_is_healthy()` validation from the command hot path. Use the durable cursor and committed marker for normal export. Run full parity validation in the existing background backfill path, with an explicit repair path on failure. | high | medium |
| 4 | Coalesce non-critical `run.json` and registry projection fsyncs. Keep raw JSONL durability and command receipts unchanged. Define the recovery point and add crash-injection tests before reducing fsync frequency. | medium-high | medium |
| 5 | Add a dedicated control-plane server or small supervisor thread for ping and health. This reduces false readiness failures, but it does not make command bodies fast. | medium | medium-high |
| 6 | Add bounded event-ingestion backpressure. Cap per-run and global pending events, yield between batches, and record queue depth. Do not let provider bursts monopolize the loop. | medium-high | medium |
| 7 | Make recovery scans tolerate a run disappearing after `list_runs()`. Skip `RunNotFound` for that run and avoid logging a full traceback. This removes repeated archive/recovery race work seen in the live log. | medium | low |
| 8 | Review command acknowledgement points. For safe operations, return after durable intent or provider admission and let the receipt finish asynchronously. Keep archive and destructive operations behind durable completion unless their contract changes. | medium | high, policy-sensitive |

The first two fixes address the shared root cause: large synchronous sections
run by tasks on the supervisor loop. PR #279 is still worthwhile, but it
should land as a throughput fix, not as the complete command-timeout fix.

## Conclusion

The command path times out because the supervisor is a single-loop reactor,
while event persistence and archive finalization contain synchronous SQLite,
JSON, fsync, and file-tree work. Reads bypass that queue or do less work, so
they often remain available. PR #279 reduces SQLite overhead but leaves the
largest loop-blocking sections in place. The top fix is to offload archive and
event persistence behind bounded, ordered workers, then add direct control
health only as a secondary safety measure.
