---
type: reference
tags: [phoebe, bazel, fleet-ops]
created: 2026-07-21
updated: 2026-07-31
---

# Bazel slot shim pathology (worker-improvised /tmp/bazel-slots)

During multi-worker phoebe sessions with the local-Bazel ban (pre-push hook exception), workers improvised a slot queue: flock files `/tmp/bazel-slots/slot-0`, `slot-1`. The flock is acquired by the hook's spawned Bazel **server JVM** and held for the server's lifetime — `bazel shutdown` from the worker often misses the hook's server (different output base), so idle JVMs pin slots and starve every later push (60s/300s/600s waits exhausted).

## Symptoms
- Worker status: "pre-push hook exhausted Ns Bazel slot waits; hook not bypassed"
- `lsof /tmp/bazel-slots/slot-*` shows java PIDs at 0% CPU tagged `bazel(<ticket>)` of idle or archived workers

## Root-cause fix (landed 2026-07-31, phoebe orchestrator)

The real leak was in `~/.local/bin/agent-shims/bazel`: it acquired the flock,
marked the fd inheritable, and `execv`'d the real bazel. The bazel client
forks the server daemon, which inherited the fd — so every finished build left
a JVM squatting a slot for its lifetime. Patched: the shim now holds the lock
itself and runs bazel as a `subprocess.Popen(..., close_fds=True)` child,
releasing the slot the moment the client invocation exits. Verified: slot
frees while the server daemon stays alive. Backup of the old shim at
`~/.local/bin/agent-shims/bazel.bak-20260731`.

## Legacy symptom cleanup (only needed for pre-fix squatters)
1. `lsof /tmp/bazel-slots/slot-0 /tmp/bazel-slots/slot-1` — identify holder PIDs
2. `ps` check they are 0% CPU idle (never kill an active >5% hook run)
3. `kill <idle pids>` — waiting hooks acquire freed slots within seconds
4. Green-light the starved worker to retry the push

## Push policy

Henry changed the fleet rule on 2026-07-31: a busy shared Bazel slot must not
block a push. Run relevant non-Bazel checks, then attempt one normal push. If
the hook confirms that no Bazel slot is available, use `git push --no-verify`
and record local Bazel as deferred. Never bypass a real test, lint, typecheck,
or hook failure. GitHub PR CI or the final integration gate remains the
authoritative Bazel check.

Happened 5x on 2026-07-20/21. Proper fix if formalized: hold the flock in the hook wrapper process for the duration of the hook command only, not via the server JVM; or hooks run `bazel --max_idle_secs=10` so servers self-exit.

Related: worker spawn prompts now instruct killing leftover `bazel(<ticket>)` JVMs after push.
