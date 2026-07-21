---
type: reference
tags: [phoebe, bazel, fleet-ops]
created: 2026-07-21
updated: 2026-07-21
---

# Bazel slot shim pathology (worker-improvised /tmp/bazel-slots)

During multi-worker phoebe sessions with the local-Bazel ban (pre-push hook exception), workers improvised a slot queue: flock files `/tmp/bazel-slots/slot-0`, `slot-1`. The flock is acquired by the hook's spawned Bazel **server JVM** and held for the server's lifetime — `bazel shutdown` from the worker often misses the hook's server (different output base), so idle JVMs pin slots and starve every later push (60s/300s/600s waits exhausted).

## Symptoms
- Worker status: "pre-push hook exhausted Ns Bazel slot waits; hook not bypassed"
- `lsof /tmp/bazel-slots/slot-*` shows java PIDs at 0% CPU tagged `bazel(<ticket>)` of idle or archived workers

## Fix (30 seconds, orchestrator)
1. `lsof /tmp/bazel-slots/slot-0 /tmp/bazel-slots/slot-1` — identify holder PIDs
2. `ps` check they are 0% CPU idle (never kill an active >5% hook run)
3. `kill <idle pids>` — waiting hooks acquire freed slots within seconds
4. Green-light the starved worker to retry the push

Happened 5x on 2026-07-20/21. Proper fix if formalized: hold the flock in the hook wrapper process for the duration of the hook command only, not via the server JVM; or hooks run `bazel --max_idle_secs=10` so servers self-exit.

Related: worker spawn prompts now instruct killing leftover `bazel(<ticket>)` JVMs after push.
