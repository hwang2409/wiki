# Supervisor Timeout Prevention — Design

Date: 2026-08-24
Status: draft, pending review
Predecessors: [supervisor-timeout-diagnosis-2026-08-19](../../../vault/wiki-app/supervisor-timeout-diagnosis-2026-08-19.md), [supervisor-optimization-audit-2026-08-20](../../../vault/wiki-app/supervisor-optimization-audit-2026-08-20.md)

## 1. Problem

Agent-facing supervisor commands (steer, spawn, archive, replace) time out under
fleet load. Reads slow down as archives accumulate. Clients that time out cannot
tell whether their write happened, because the server usually completes it after
the client gives up.

Root causes, verified on main at `67ae2f9c`:

1. **The dispatch loop runs blocking work.** One asyncio loop serves the Unix
   socket, the command reactor, and provider event persistence. Event
   persistence is synchronous on that loop: ~8 fsyncs per raw+normalized event
   pair, atomic `run.json` rewrites, SQLite materialization, JSON encoding
   (`supervisor.py` `_dual_write_normalized`, `store.py:595-705`). While any of
   it runs, nothing dispatches — including `ping`.
2. **One global FIFO command queue with completion-based acknowledgement.**
   `CommandQueue` runs a single `global-command-reactor` task; `submit()`
   resolves only after the provider effect and receipt persist
   (`command_queue.py:202,224-227,463-501`). One slow command delays every
   agent's commands (cross-agent head-of-line), and clients spend their entire
   timeout budget waiting for completion of work that keeps running server-side.
3. **Unbounded provider ingress.** Codex/Claude adapters queue events without
   count or byte bounds; the supervisor consumes serially per run. A busy worker
   burst monopolizes the loop.
4. **O(history) reads and replays.** `GET /api/agents` verifies every archived
   session before returning 20 rows (0.64s at 1k sessions, ~5s projected at 8k).
   The 1s recovery loop replays each wk status log in full every pass. Boot
   re-scans archives multiple times.
5. **Incoherent stacked timeouts.** MCP sidecar → HTTP (15s) → Unix socket (3s
   fast reads) → loop. Inner budgets do not nest inside outer ones, and no layer
   owns retry or post-timeout verification.

## 2. Goals

- No agent-facing command is ambiguous after a client timeout: every write is
  identified, idempotent, and verifiable by read.
- Command acknowledgement latency is bounded by disk-intent persistence, not by
  provider effects: **accepted p99 < 500ms under load**.
- `ping` and status reads stay responsive regardless of command/persistence
  load: **ping p99 < 50ms** while the fleet is busy.
- One slow agent cannot delay another agent's commands.
- Read latency is independent of archive count.
- One client path for Claude and Codex sessions, with retry/verify built in.
- A load gate in the repo prevents regression of the above numbers.

Non-goals:

- Multi-host or remote operation. Everything remains localhost.
- Changing durability guarantees. Raw JSONL append, command intents, and
  receipts keep their current fsync policy.
- Rewriting the supervisor's process model (still one supervisor process).

## 3. Design principles

These are the invariants that make the fix permanent rather than a patch:

- **P1 — The dispatch loop never blocks.** No fsync, SQLite call, file copy,
  tree walk, or large JSON encode executes on the supervisor's event loop. The
  loop admits work, orders it, and publishes results.
- **P2 — Acknowledge at durable intent.** A command is "accepted" once its
  intent is fsynced to the command log. Completion is a separate, pollable
  fact (the receipt). Only destructive operations may opt into
  completion-based response, and then with an explicit longer contract.
- **P3 — Order per agent, not globally.** Commands serialize per agent id.
  Cross-agent ordering is not a feature anyone uses; stop paying for it.
- **P4 — Reads never scan unbounded history.** Any read that today walks all
  archives or replays a full log must consume a durable, incrementally-updated
  projection instead.
- **P5 — Exactly one client stack, and it owns ambiguity.** The client library
  (CLI) generates request ids, retries idempotently, and verifies by read after
  timeout. No layer above it re-implements timeouts.

## 4. Server-side changes

### S1. Writer executor (WIKI-358)

Move the synchronous persistence bundle (JSONL appends + fsyncs, `run.json`
projection writes, SQLite materialization) off the loop into a bounded
`ThreadPoolExecutor` owned by the supervisor. The loop-side event pump submits
persistence jobs and awaits futures; per-run ordering is preserved with the
existing per-run lock (jobs for one run execute in order; different runs may
interleave). Persistence failures propagate back to the event pump exactly as
today.

Executor bound: small and fixed (2-4 threads). The point is isolation from the
loop, not parallel disk throughput. Queue depth is exported for observability.

This is diagnosis rank 1+2 and directly buys the ping SLO. Worktree already
staged at `.worktrees/wiki-358-writer-executor`.

### S2. Command admission rework: per-agent lanes + accepted response

Two changes to `CommandQueue`:

1. **Per-agent lanes.** Replace the single `global-command-reactor` with one
   lane (queue + worker task) per agent id, mirroring the structure that
   command-scoped recovery already uses (`_scoped_recovery_queues`). Boot
   recovery keeps its global FIFO for stable replay order.
2. **Accepted response.** `submit()` gains an admission mode: for
   non-destructive commands (steer, spawn, autopilot toggles), the socket
   response returns `{status: "accepted", command_id, agent_id}` immediately
   after `append_intent` fsyncs. Execution and receipt persistence continue in
   the lane. A new `command/status` read returns the receipt state for a
   `command_id` (pending / running / success / failed, with result or error).
   Destructive commands (archive, replace) stay completion-based by default;
   callers may pass `wait=false` to opt into the accepted contract explicitly.

The existing `request_id` idempotency (intent replay returns the persisted
receipt, conflicts rejected) is the foundation that makes this safe and makes
client retries safe; it does not change.

### S3. Durable archive catalog (WIKI-363)

Maintain a catalog (single JSON or SQLite table in the runtime dir) of archived
sessions: id, role, run ids, commit state, timestamps. Update it in the same
step that makes the archive marker durable. `GET /api/agents` and boot
reconciliation read the catalog and stop verifying every archive tree on the
hot path. Full verification moves to the existing background backfill task,
with a repair path that rebuilds the catalog by scanning (the catalog is a
projection; scanning remains the recovery of last resort).

### S4. wk replay gating (WIKI-364)

Gate wk status rebuilds on source log revision (size + mtime or an explicit
sequence). The 1s recovery loop replays a wk log only when it changed since the
last pass.

### S5. Bounded provider ingress

Cap provider adapter queues by item count and total bytes. When a queue is
full, the adapter's reader thread blocks (backpressure to the provider pipe)
rather than growing memory. Export queue depth, oldest-event age, and
blocked-reader time. The supervisor's per-run serial consumption is unchanged.

### S6. Timeout budget coherence

With the CLI as the only client (section 5), budgets nest once and are defined
in one place (the CLI):

- accepted-mode write: client budget 5s (covers socket + intent fsync).
- completion-mode write (archive/replace): client budget 60s, with progress
  from `command/status` polling instead of one long silent wait.
- reads and ping: 3s.

The backend HTTP layer and socket client stop layering their own competing
timeouts; they use what the caller passes.

## 5. Client-side: retire MCP, ship `wk agent`

### What exists today

Every session (Claude and Codex) is spawned with a per-session MCP sidecar
process (`wiki-backend --wiki-artifacts-mcp`, ~40MB each) exposing
`wiki_agent_tools.py` (list/read/steer/spawn/archive/next-review/autopilot) and
`wiki_artifacts.py` (render_artifact). The sidecar translates stdio JSON-RPC to
HTTP calls against `WIKI_BACKEND_URL`.

### Replacement

Extend the repo CLI with an `agent` command group and an `artifact` command,
callable via Bash from any session:

```
wk agent list
wk agent status <id>
wk agent events <id> [--since N]
wk agent spawn --ticket T --kind cdx|cc [...]
wk agent steer <id> --message - [--wait]
wk agent replace <id> [...]
wk agent archive <id>
wk agent next-review
wk agent autopilot enable|disable|status|ack-merge
wk artifact render --kind mermaid file.mmd
wk command status <command_id>
```

Contract:

- **Env:** reads `WIKI_BACKEND_URL`, `WIKI_AGENT_ID`, `WIKI_RUN_ID`,
  `WIKI_AGENT_RUNTIME_DIR` — the same variables the sidecar receives today, so
  the spawn env shrinks rather than changes shape.
- **Idempotency:** the CLI generates a `request_id` per invocation (uuid7) and
  sends it with every write. On timeout or connection reset it retries the
  identical request once; the server's intent replay makes this exact-once.
- **Verify-after-timeout:** if the retry also fails, the CLI queries
  `wk command status` / `wk agent status` and reports the true outcome with a
  distinct exit code (0 = confirmed applied, 1 = confirmed failed,
  2 = unknown/needs human) instead of a bare timeout.
- **Waiting:** writes default to accepted-mode (return `command_id`
  immediately). `--wait` polls `command/status` to completion. Archive and
  spawn default to `--wait` (see section 9 for spawn's reason).
- **Output:** JSON on stdout (agents parse it fine and it stays greppable);
  `--quiet` for exit-code-only use in scripts.

### What dies

- The per-session MCP sidecar processes and their spawn wiring
  (`--wiki-artifacts-mcp` mcp-config blocks in the Claude/Codex spawn paths).
- `wiki_agent_tools.py` (the CLI calls the same backend HTTP routes directly).
- MCP schema maintenance for agent tools.

`wiki_artifacts.py` rendering logic survives — the `wk artifact` command posts
to the same backend route; only the MCP transport wrapper is deleted.

### What agents see

Orchestrator/worker contracts and skills reference `wk agent ...` commands
instead of MCP tool names. A short vault note documents the surface; `wk agent
--help` is the canonical reference. Permissioning is a Bash allowlist entry.

## 6. Enforcement: the load gate

A regression test target (`make load-gate`) that the merge gate runs for PRs
touching `backend/app/agent_runtime/`:

- Spins an isolated supervisor (temp runtime dir) with N=8 synthetic runs.
- Drives: sustained provider event bursts (1k events/s aggregate, 512-byte
  payloads), one archive per 10s, steer commands round-robin at 5/s, and a
  concurrent ping loop at 10/s.
- Asserts over a 60s window: ping p99 < 50ms, steer accepted p99 < 500ms, no
  request error other than deliberate faults, adapter queue bytes below the S5
  cap.
- Fails loudly with the offending percentile, so the number is the contract.

This is what makes the fix "for good": P1-P5 are enforced by a failing gate,
not by review vigilance.

## 7. Migration and cutover

Order chosen so each step is independently shippable and observable:

1. **S1 writer executor** (WIKI-358, staged) — biggest single win; unblocks the
   ping SLO.
2. **S2 per-agent lanes + accepted response + `command/status`** — server
   contract for the CLI. Backward compatible: default remains completion-based
   until callers pass the new flag; MCP sidecar keeps working during rollout.
3. **CLI `wk agent`/`wk artifact`/`wk command`** — lands alongside the still
   present MCP path; orchestrator contracts and skills switch to it; one fleet
   arc runs on the CLI to shake it out.
4. **Remove MCP sidecar wiring** — delete spawn-path mcp-config, sidecar flag,
   and `wiki_agent_tools.py`.
5. **S3 archive catalog, S4 wk gating, S5 ingress bounds** — land in any order
   behind the load gate.
6. **Load gate** wired into the merge gate no later than step 4.

Rollback story: steps 2-3 are additive; step 4 is a small revert if the CLI
misbehaves in the field.

## 8. Ticket map

| change | ticket |
| --- | --- |
| S1 writer executor | WIKI-358 (merged, PR #289) |
| S2 per-agent lanes + accepted ack + command/status | WIKI-376 |
| S3 archive catalog | WIKI-363 (exists) |
| S4 wk replay gating | WIKI-364 (exists) |
| S5 ingress backpressure | WIKI-378 |
| CLI `wk agent` + retire MCP | WIKI-379 (add CLI), WIKI-380 (remove MCP) |
| load gate | WIKI-381 |
| parity backfill holds run lock across O(n^2) compare, starving archives (root cause 6, found 2026-08-24 evening via py-spy) | WIKI-377 |

## 9. Risks and open questions

- **Accepted-mode semantics for spawn.** Spawn's result (the new run id) is
  produced by execution, not admission. Accepted-mode returns `command_id`
  only; callers that need the run id poll `command/status`. The CLI's `spawn`
  defaults to `--wait` for this reason; orchestrators that batch-spawn can opt
  out.
- **Per-agent lanes change global ordering.** Nothing observed depends on
  cross-agent command order (recovery already uses per-agent lanes), but the
  load gate includes an interleaved-agent scenario to catch surprises.
- **Codex CLI ergonomics.** Codex workers already run shell commands well;
  the risk is prompt-contract drift, mitigated by making `wk agent --help`
  authoritative and updating the worker-contract templates in the same PR as
  step 3.
- **Backpressure vs. provider stalls (S5).** Blocking an adapter reader thread
  applies backpressure to the provider pipe; a pathological provider could
  stall a run. Bounds are set high (10k events / 64MB per run) so this only
  engages in runaway cases that today would OOM instead.
