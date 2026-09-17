---
type: reference
tags: [wiki-app, supervisor, perf]
created: 2026-08-20
updated: 2026-08-20
---

# Supervisor optimization audit (2026-08-20)

Produced by WIKI-361 (one-shot sol audit worker, xhigh). Follow-up to [[supervisor-timeout-diagnosis-2026-08-19]] after #284/#285 merged rank 1 (archive-finalize offload). Source: main at 8b17993. Ticketed from this audit: WIKI-362 (build guard), WIKI-363 (archive catalog), WIKI-364 (wk replay gating). WIKI-358/359/360 cover the earlier ranks.

## result

the largest new push-path issue is the native build guard. it requires both runtime locks before any build work starts.

a warm staged build took 45.86 seconds. a clean staged build took 92.56 seconds. this work can run while Wiki.app stays live.

the clearest backend read issue is archive enumeration. `GET /api/agents` validates every archive before returning only 20 rows.

the clearest new supervisor churn is wk status replay. the one-second recovery loop replays each wk log on every pass.

## measurement method

- all synthetic data used isolated directories under `/private/tmp`.
- archive tests used 100, 500, and 1,000 committed sessions. each session had eight small artifacts.
- raw-history tests used 15 runs and 300.0 MiB of valid JSONL.
- queue tests enqueued 100,000 provider events with a 512-byte text field.
- cost tests used 1,000 run checkpoints with one dirty run.
- command tests put a fast command behind one unrelated 250 ms command.
- warm medians use three runs unless this report says otherwise.
- a five-second read-only `sample` inspected the live supervisor. it did not send requests.

the live runtime had 14 run directories during the audit. its run files totaled 372.9 MB.

the live raw JSONL totaled 29.7 MiB. its integrity scan took 0.0714 to 0.0846 seconds.

## ranked optimizations

| rank | change | impact | effort | evidence |
|---:|---|---|---|---|
| 1 | move the native build guard to the swap boundary | high. removes the full build from app and supervisor downtime | small | the guard runs before all build work at `scripts/build-native-app.sh:12-17`. build work runs at `scripts/build-native-app.sh:54-101`. the safe swap still takes both locks at `scripts/native_swap_transaction.py:501-542`. measured staged builds took 45.86 seconds warm and 92.56 seconds clean. |
| 2 | add a durable archive catalog, updated at archive commit | high. targets `GET /api/agents` and supervisor boot | medium | `backend/app/main.py:1810-1822` verifies every session. `backend/app/main.py:1941-1986` reads every candidate before slicing to 20. verification fsyncs and walks each tree at `backend/app/agent_runtime/archive_protocol.py:237-317`. measured list time was 0.0544 seconds for 100 sessions, 0.3312 for 500, and 0.6419 for 1,000. without manifest verification, 1,000 took 0.1173 seconds. |
| 3 | gate wk status rebuilds by source revision | high for wk fleets. removes one full replay per run each second | small to medium | the recovery timer runs every second at `backend/app/agent_runtime/daemon.py:105-117`. each pass calls wk rebuild at `backend/app/agent_runtime/supervisor.py:3007-3009`. rebuild loads and sorts the full log at `backend/app/agent_runtime/store.py:3688-3722`. replaying 200,000 events from a 25.4 MiB log took 1.0356 seconds. |
| 4 | persist raw-integrity and normalization coverage checkpoints | medium to high on large histories. removes remaining pre-bind O(history) scans | medium to large | event-store setup parses every raw row at `backend/app/agent_runtime/event_store_migration.py:108-131` and `backend/app/agent_runtime/event_store_router.py:61-65`. orphan recovery loads both full logs at `backend/app/agent_runtime/supervisor.py:3097-3112`, then reloads normalized rows at `backend/app/agent_runtime/supervisor.py:3147-3155`. one raw pass over 300.0 MiB took 0.4720 seconds. the current orphan pass has at least two JSONL passes and larger allocations. |
| 5 | bound provider ingress by event count and byte count | medium to high during provider bursts. prevents unbounded memory growth | medium | Codex and Claude create unbounded queues at `backend/app/agent_runtime/codex.py:107` and `backend/app/agent_runtime/claude.py:168`. their readers keep putting events at `backend/app/agent_runtime/codex.py:203-213` and `backend/app/agent_runtime/claude.py:263-273`. the supervisor consumes each run serially at `backend/app/agent_runtime/supervisor.py:1160-1182`. 100,000 queued events used at least 39.0 MiB and took 0.3946 seconds to enqueue. |
| 6 | save only dirty cost checkpoints | medium. cuts five-second background disk bursts | small | one scanned run enters `dirty_run_ids` at `backend/app/agent_runtime/costs.py:963-965`. `_save_state` ignores that set and rewrites every checkpoint at `backend/app/agent_runtime/costs.py:262-300`. with 1,000 runs and one dirty run, it made 1,001 atomic JSON writes and took 0.2164 seconds. |
| 7 | coalesce `run.json` and registry projection writes | medium after WIKI-358. lowers write amplification and disk contention | medium | JSONL append uses one fsync at `backend/app/agent_runtime/store.py:687-704`. each atomic projection write uses three fsyncs at `backend/app/agent_runtime/store.py:595-611`. raw and normalized append each write both forms at `backend/app/agent_runtime/store.py:2714-2741` and `backend/app/agent_runtime/store.py:2743-2842`. the benchmark counted 800 fsyncs for 100 events. real time was 0.0609 seconds versus 0.0538 seconds with fsync stubbed. |
| 8 | return a durable accepted response before command completion | medium. removes provider-effect time from client acknowledgement | medium to large | intent persistence ends at `backend/app/agent_runtime/command_queue.py:194-209`, but `submit` waits for the final future at line 215. the worker persists the final receipt at lines 253-264. an unrelated fast command waited 0.2414 seconds behind a 0.2500-second effect because one global queue serves all agents at lines 48-65. |
| 9 | tail child transcripts from the saved source offset | medium for Claude subagent runs | medium | every parent provider event calls child sync at `backend/app/agent_runtime/supervisor.py:1402-1405`. child sync reopens and parses each full child transcript at `backend/app/agent_runtime/supervisor.py:567-633`. the materializer skips known sequence values only after parsing at `backend/app/agent_runtime/event_store_shard.py:1100-1127`. reparsing 100,000 rows from 29.9 MiB took 0.0796 seconds. |
| 10 | add a separate, minimal health lane | low to medium. improves diagnosis during loop stalls, but not command throughput | medium | every socket request awaits the same dispatch loop at `backend/app/agent_runtime/protocol.py:204-225`. ping itself is small at `backend/app/agent_runtime/supervisor.py:6048-6053`. the live five-second sample was 87.5% idle in `kevent`, so it showed no current health-lane stall. it did show 94 main-thread samples in rename and 14 in fsync. |

## `GET /api/agents`

the route always calls `list_archived()` at `backend/app/main.py:2513-2518`.

the default list path verifies all sessions. it also reads all selected bodies before it applies the limit.

at 1,000 synthetic sessions, the route's archive helper returned 20 rows in 0.6419 seconds.

manifest verification used about 0.5246 seconds of that result. this is 81.7% of the helper time.

linear estimates are 1.28 seconds at 2,000 sessions and 5.14 seconds at 8,000 sessions.

these estimates match the reported 1.3 to 5.0 second range. they are estimates, not live request measurements.

the same archive work also affects boot. `RunStore` reconciles archives twice at `backend/app/agent_runtime/store.py:767-769`.

each reconciliation validates every archive at `backend/app/agent_runtime/store.py:1725-1737`.

supervisor construction scans them again for workgraph repair at `backend/app/agent_runtime/supervisor.py:547-555` and `backend/app/workgraph_service.py:376-393`.

the catalog should store the latest rows, run IDs, roles, and commit state. update it after the marker becomes durable.

recovery can rebuild the catalog in a background repair task. normal reads should not fsync archive directories.

## boot and storage findings beyond WIKI-360

WIKI-360 owns `RunStore` projection rebuild. current main still has later pre-bind work.

`EventStoreRouter` parses every raw JSONL before the socket binds. the orphan sweep then reads raw and normalized JSONL again.

wk status replay also runs before bind. workgraph archive reconciliation runs during `Supervisor` construction.

the server starts only after construction and the first recovery pass at `backend/app/agent_runtime/daemon.py:157-170`.

the cost scan already skips terminal hot runs at `backend/app/agent_runtime/costs.py:1014-1017`.

it also checks only active runs on stable roots at `backend/app/agent_runtime/costs.py:1024-1039`.

therefore, the old "skip terminal costs scans" idea is already present on current main.

the remaining cost problem is checkpoint output. every changed refresh rewrites every saved run checkpoint off-loop.

cost refresh uses `asyncio.to_thread` at `backend/app/agent_runtime/costs.py:1072-1081`. it does not block the API loop directly.

however, its 1,001 atomic writes can compete for the same disk used by the supervisor.

the command journal is not a current size problem. the live database was 348,160 bytes with 78 command events.

its WAL and `synchronous=FULL` policy is correct for durable intent at `backend/app/agent_runtime/command_log.py:58-68`.

event shards use WAL and `synchronous=NORMAL` at `backend/app/agent_runtime/event_store_shard.py:719-735`.

the missing `intent_sequence` index affects only pending recovery at `backend/app/agent_runtime/command_journal.py:255-270`.

the live journal had zero pending intents. add that index with a future journal retention ticket, not as a top ticket.

## verdict on prior ranks 4 through 8

### rank 4: coalesce non-critical fsyncs — survives

current main still performs eight fsync calls for a normal raw plus normalized event pair.

WIKI-358 can move this work off-loop. it does not reduce the write count or disk pressure.

the live sample also caught rename and fsync work on the main Python thread.

keep raw JSONL and command intent durability. batch only rebuildable projections and registry snapshots.

### rank 5: dedicated control-plane health lane — survives, but down-ranked

#285 restarts a failed listener. it does not isolate ping from the event loop.

a separate lane can report process health during an event-loop stall. it cannot make blocked commands complete.

it also cannot help current boot. no listener exists until after `recover_on_start()`.

do this after WIKI-358 and the pre-bind boot work. those changes remove more user-visible delay.

### rank 6: bounded event-ingestion backpressure — survives

both main provider adapters still use unbounded queues. the supervisor still processes one event at a time per run.

WIKI-358 raises sustainable throughput. it cannot set a memory ceiling during a larger burst.

bound both item count and bytes. export queue depth, oldest age, and blocked-reader time.

### rank 7: tolerate disappearing runs in recovery — superseded

the exact race fix is present on current main.

auto-archive catches `RunNotFound` at `backend/app/agent_runtime/supervisor.py:1146-1158`.

the lost-run reaper catches it at `backend/app/agent_runtime/supervisor.py:3597-3622`.

do not open another ticket for that race. the repeated wk replay is a different recovery-loop ticket.

### rank 8: acknowledge after durable intent or admission — survives

the queue persists intent before execution. the caller still waits for provider work and final receipt persistence.

the global FIFO also creates cross-agent head-of-line delay. the benchmark reproduced that delay directly.

first add an accepted response and a receipt-status contract for non-destructive commands.

keep archive, replace, and other destructive calls completion-based until their API contract changes explicitly.

## Wiki.app push path

the current default path is `make native-build`, which calls `scripts/build-native-app.sh` at `Makefile:36-37`.

the build guard checks `app.lock` and `supervisor.lock` before staging. it refuses immediately while either owner is live.

this design makes the safe build work occur after the operator quits the app and stops the supervisor.

the measured avoidable outage is 45.86 seconds for a warm rebuild. the clean build took 92.56 seconds.

the warm build included a 12.77-second incremental Rust release build. PyInstaller and frontend work used most remaining time.

the clean Rust release build took 47.29 seconds.

`FORCE_STAGE_ONLY=1` already proves the build can run while the runtime stays live.

the final swap has the correct lock boundary. it takes `app.lock`, snapshots runs, and stops the old supervisor.

it then takes `supervisor.lock`, swaps the bundle, restarts the daemon, and starts recovery at `scripts/native_swap_transaction.py:501-587`.

the stop helper can wait 15 seconds at `scripts/native_swap_transaction.py:227-251`.

handover polls ping, then polls every saved run in sequence. it needs two stable passes at `scripts/native_swap_transaction.py:401-465`.

the new supervisor does not bind until all pre-bind work finishes. this adds the measured history and replay costs after swap.

the correct push shape is: build and sign while live, acquire locks for the swap, then recover and verify.

## top three next tickets

1. move the native guard to the swap boundary. this removes 45.86 seconds of measured warm-build downtime. -> WIKI-362
2. add a durable archive catalog. this attacks both the 1.3-5.0 second API symptom and repeated boot scans. -> WIKI-363
3. gate wk recovery by source revision. one 25.4 MiB log already takes longer than the one-second loop interval. -> WIKI-364

## confidence and limits

the file-path findings are direct observations of current main.

the synthetic timings show scaling and isolate causes. APFS cache state and file shapes can change absolute values.

the live runtime was small and active. the five-second sample did not capture a stall.

no end-to-end live swap ran because that would modify the installed app and shared supervisor state.
