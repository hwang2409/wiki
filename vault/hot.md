---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-25
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **SUPERVISOR PERF ARC LANDED + SWAPPED (2026-08-25 ~14:52Z), THIRD FRESH-SLATE WIPE (~14:54Z, Henry-requested):** all timeout-prevention spec tickets implemented in one arc and live: WIKI-377 (parity-backfill lock release + bounded replace_run_from — the real 2026-08-24 outage cause; wait4 theory was WRONG, py-spy proved it), WIKI-364 (wk replay revision gate), WIKI-375 (size-checkpointed O(1) boot + projection-existence probe; terminal runs still repair when dirty), WIKI-359 (export cursor gate), WIKI-363 (archive catalog at commit), WIKI-376 (per-agent command lanes replace global FIFO + `wait:false` accepted-at-intent ack; `idempotency/status` is the receipt poll), WIKI-378 (bounded ingress 10k/64MB, forced puts on shutdown paths), WIKI-381 (`make load-gate`, PASS: ping p99 ~0ms, steer accepted p99 15.7ms; wired into orch gate contract). Full RCA: [[archive-stall-recurrence-2026-08-24]].
- **ALL OF IT IS UNCOMMITTED working-tree state** — commit series per ticket is the immediate next step. Gate at swap time: 2269 passed; only pre-existing reds (see below) + pre-existing event_store_supervisor flake (reproduced on origin/main).
- **Phoebe fleet resumed 2026-08-25 ~15:00Z** (Henry: "resume everything"). Three fresh cdx luna fix rounds spawned from archived sol verdicts: PHO-16929-PR3 (#15244, 7 blocking + 1 high: extension poll PHI leak, dedupe ON CONFLICT R10, realtime field mismatch, Unicode redaction bypass), PHO-16437-PR4 (#15133, 5 blocking: cooperative enforcement, unclaimed lease cancel, R9/R11 lock-order deadlock), PHO-16956-PR3 (#15253, 3 blocking + 1 high: Slack Connect org routing, R10 rolling-deploy leak, grant workflow unreachable; review pinned 53d32f0f, head had moved to 25cce2bf). Watchlist + fleet monitor armed. PHO-16593 #15003 merge-ready (REVIEW5 clean, head 4427115) — waits on lead approval then Henry merge auth; PR watcher armed.
- Pre-wipe queue context elsewhere: tooling was NEWT-52-REVIEW9/NEWT-62-PR7 mid-arc; wiki next = WIKI-359 (now DONE in-tree); zeta orch live again 14:54Z.
- **Phoebe workers (never merge):** respawn is Henry's call (authorized for this round 2026-08-25).
- Primary phoebe checkout on `henry/github-sweep-422-fix`: uncommitted `shift_confirmations/notifications.py` edit + untracked v3-invariance workflow yaml + write-tools audit note — Henry's in-progress work, untouched.

## Recent facts

- Supervisor now idles at ~0.4% CPU on fresh runtime (was ~100%: the O(n^2) parity sweep + O(history) loops). Boot on wiped runtime: seconds. First boot over PRE-checkpoint run.json files still pays one full reconcile to stamp checkpoints; every later boot is O(1).
- Pre-existing main reds (cite as baseline, not worker breakage): `test_agent_runtime_store.py::ProtocolFixtureTests::test_codex_failed_render_completion_preserves_write_time_event` and `test_accounts.py::AuthDeadAttemptCapTests::test_stops_reviving_after_max_attempts` (verified on clean origin/main worktree 2026-08-25). `test_native_build_guard::test_interrupt_after_exchange...` and `test_event_store_supervisor::test_failed_rebuild_does_not_reattach...` are pre-existing flakes.
- `unknown-kind-telemetry.json` rehydrated itself within seconds of the fresh boot — WIKI-382's mystery source is live code, not stale state.
- Agent_runtime PRs now owe `make load-gate` in the local gate ([[orchestrator-worker-protocol]] 2i).
- Stage dirs: swapped stages self-clean; older ones in `.native-build-staging/` still need manual sweep. Stale worktrees/branches cleanup still pending.
- Command-log backups from tonight's surgeries: `/tmp/command-log.backup-*.sqlite3`.
- Spawn HTTP API rejects codex spawns without `effort` ("Reasoning effort is required for Codex workers") — always pass it.
- `GET /api/agents/<id>` is 404; scriptable read path is `~/me/fun/wiki/wiki agent status <id>` (full path — `wiki` not on PATH in orch shells).
- Reviewer verdict bodies survive archive in `~/me/fun/agent-archive/<TICKET>/<ts>/events.jsonl` (`item_completed` params.item.text) — fix contracts can be rebuilt after a full fleet archive.

## Watchouts

- **Supervisor writes: HTTP-first is the RULE (Henry 2026-08-24c)** — client `request_id` on every write; `wait:false` now available for accepted-at-intent steers. [[orchestrator-worker-protocol]].
- Fresh-slate recipe (3rd use): quit app -> SIGKILL supervisor if it survives -> wipe `~/.wiki/agent-runtime` contents keeping `*.lock` -> clear /tmp registry/status -> relaunch. Backup command-log first.
- Archive of a run mid-events-rebuild waits briefly; a lock conflict now fails fast with retryable StoreConflict instead of wedging (WIKI-377).
- Stray watchlist loops outlive sessions — sweep `ps ax | grep '[w]atchlist'`.
- `~/.codex/sessions/` dirs can lose owner rx bits — `chmod u+rx`.
- GitHub checks absent on wiki + zeta PRs (billing) — local gate authoritative.
- Merge authority: wiki + zeta + tooling = orch merges after clean pass. Phoebe: never.
