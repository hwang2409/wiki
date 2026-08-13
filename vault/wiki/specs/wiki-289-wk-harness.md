---
type: reference
tags: [wiki-app, architecture, agents, harness]
created: 2026-08-13
updated: 2026-08-13
---

# WIKI-289: wk harness v0

**Decision:** Add an opt-in, wiki-native harness named `wk`. It has two plan-authed lanes: Claude Agent SDK and Codex App Server. A shared loop owns tools, integrity records, status, session state, and normalized events. `cc` and `cdx` remain the default paths.

This is a design for v0. It does not add provider API calls, API keys, or a new paid service. It does not replace `cc` or `cdx`.

## 1. Goals and non-goals

### Goals

- Run review workers through a small, testable loop owned by Wiki.
- Use plan billing only.
- Give both lanes the same tool boundary and event format.
- Make gate results, file mutations, and status transitions harness facts.
- Preserve supervisor parity for spawn, steer, status, replace, resume, interrupt, and archive.
- Target the WIKI-282 normalized SQLite event view from the first event design.
- Measure reviewer quality and cost for about two weeks before expansion.

### Non-goals

- Do not expose `wk` by default.
- Do not send prompts through a direct Anthropic or OpenAI API client.
- Do not give Claude or Codex the provider's default file or shell tools.
- Do not make `wk` the default for plan, implement, or orchestrator roles.
- Do not remove `cc`, `cdx`, native transcript parsing, or JSONL archives in v0.
- Do not add a separate daemon or remote RPC service for the harness.

## 2. Current boundaries

The current API accepts worker kinds only as `cc` or `cdx`. Worker validation rejects other kinds in `backend/app/main.py:5071-5077`. The request model also applies provider-specific effort rules in `backend/app/main.py:4334-4337`. The orchestrator model has the same closed kind set in `backend/app/main.py:4358-4367`.

The current spawn path maps `cdx` to Codex and `cc` to Claude, then sends one `run/start` command to the supervisor (`backend/app/main.py:5154-5185`). The supervisor creates a `RunRecord`, injects the runtime card, stores the run, constructs an adapter, starts it, and commits the start (`backend/app/agent_runtime/supervisor.py:2402-2534`). The factory selects `CodexAppServerAdapter` or `ClaudeStreamAdapter` from the provider enum (`backend/app/agent_runtime/factory.py:1-27`).

The provider-neutral adapter already exposes start, resume, steer, interrupt, replace, status, event streaming, archive, and response operations (`backend/app/agent_runtime/provider.py:27-148`). The supervisor event pump serializes each run's provider events under an event lock (`backend/app/agent_runtime/supervisor.py:901-939`). It appends raw input first, normalizes it, appends the normalized envelope, and only then publishes the session invalidation (`backend/app/agent_runtime/supervisor.py:1030-1179`). The raw append is fsynced before normalization (`backend/app/agent_runtime/store.py:2398-2425`).

The Claude adapter currently starts the `claude` stream-json subprocess and sends the kickoff prompt (`backend/app/agent_runtime/claude.py:134-207`, `backend/app/agent_runtime/claude.py:737-752`). The Codex adapter currently starts `codex app-server --stdio`, sends JSON-RPC lines, and tracks request and server-request IDs (`backend/app/agent_runtime/codex.py:70-123`, `backend/app/agent_runtime/codex.py:202-235`, `backend/app/agent_runtime/codex.py:264-328`). `wk` will add new adapters behind the same supervisor contract. It will not silently change either existing adapter.

The current transcript parser keeps mutable pending-tool state, stable event IDs, a bounded change log, and a dirty boundary in process memory (`backend/app/transcripts.py:6039-6097`, `backend/app/transcripts.py:6348-6383`). It assigns a stable ID when a visible event is appended and emits patches when tool state changes (`backend/app/transcripts.py:3945-3997`). WIKI-282 moves this view into SQLite while keeping raw JSONL as ground truth. Its design is at `../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md`, commit `5116712f88bf4c5e90f3c08621afeec520e11cfa` in the local WIKI-282 worktree.

## 3. Architecture

```text
supervisor
  ├─ wk admission and feature flag
  ├─ wk adapter: Claude Agent SDK
  └─ wk adapter: Codex App Server
         │
         ▼
     shared wk core
       ├─ turn loop and steering queue
       ├─ typed tool registry
       ├─ integrity ledger and event sink
       ├─ session tree and compaction policy
       ├─ atomic status writer
       └─ WIKI-282 ingest envelope
         │
         ├─ raw.jsonl and events.jsonl during migration
         └─ normalized SQLite view through the WIKI-282 materializer
```

### 3.1 Shared core

The shared core is a single-writer state machine per run. The supervisor owns admission, lifecycle, archive, and process identity. The core owns the provider turn, tool calls, steering queue, session cursor, status writes, and harness events.

The core has these interfaces:

| Interface | Responsibility | Durable fact |
| --- | --- | --- |
| `WkProvider` | Start, resume, send, interrupt, replace, close, and stream provider messages | Provider request and response IDs |
| `WkTool` | Validate input and execute one named Wiki tool | Start, finish, error, and output metadata |
| `WkEventSink` | Assign source sequence and publish one event envelope | Event sequence and causal parent |
| `WkSession` | Append messages, records, lanes, and compaction entries | Session entry or record |
| `WkStatusWriter` | Write the worker status file | Atomic status version |
| `WkRunControl` | Map supervisor operations into core operations | Idempotent command receipt |

The core never reads a provider transcript to decide whether a tool ran. A provider message can request a tool. Only the tool registry can execute it. The core records both the request and the result.

### 3.2 Claude lane

The Claude lane uses the Claude Agent SDK and its Claude Code runtime. It uses the existing Claude plan-auth path. It does not use an Anthropic API key.

The SDK session receives:

- The Wiki system prompt.
- The worker kickoff prompt.
- A tool list containing only Wiki-owned tools.
- A working-directory and run context.
- A cancellation signal owned by the supervisor.

The SDK's default tools are disabled. The lane registers Wiki implementations for `read`, `write`, `edit`, `bash`, `gate`, and `status`. The lane converts SDK assistant, tool, result, control, compaction, and error messages into the shared event envelope.

The SDK integration must use the SDK's plan-authenticated Claude Code runtime. It must fail at startup when the runtime would require an API key or when a default provider tool is active. The failure is a blocked run with a recorded reason.

### 3.3 Codex lane

The Codex lane is a thin driver over `codex app-server --stdio`. It uses Codex plan auth. It does not call an OpenAI API directly.

The driver owns:

- JSON-RPC request IDs and response matching.
- Server-request approval and user-input matching.
- Thread and turn lifecycle translation.
- Provider event ordering.
- Translation into the shared `WkProviderMessage` shape.

The driver must negotiate a tool policy that routes file and shell execution through the shared Wiki tool registry. If the installed App Server cannot prove that policy, the lane stays disabled and records `wk.codex.tool_policy_unavailable`. It must not claim harness integrity while provider-native tools can bypass the ledger.

The protocol parser should be extracted from the current Codex handling rather than invented again. The current adapter already handles JSON-RPC request/response matching, approval waits, and lifecycle states (`backend/app/agent_runtime/codex.py:282-451`). WIKI-282 also requires the inspector for provider events to remain separate from the rendered view (`../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md:304-306`).

### 3.4 Turn and steering loop

Each provider response produces one or more assistant messages. The core then:

1. Validates the assistant message.
2. Records any tool calls.
3. Executes the tool calls through the shared registry.
4. Records tool results, including failures.
5. Adds results to the session tree.
6. Checks the steering queue.
7. Starts the next provider turn or ends the run.

`send_now` maps to a steering message. `send_on_idle` maps to a follow-up message. A steer never edits the provider transcript outside the core. A pending steer remains durable until its echoed user event or an explicit failure receipt proves delivery.

## 4. Wiki-native event format

Both lanes emit the same source envelope. Provider-specific data stays inside `payload`. The WIKI-282 materializer maps `source_seq` to `raw_seq`, assigns the stable integer `event_id`, and stores the full envelope in `events.event_json`.

### 4.1 Envelope

```json
{
  "schema": "wiki.wk.event.v0",
  "source_event_id": "01J...",
  "source_seq": 42,
  "run_id": "run-uuid",
  "agent_id": "WIKI-289-REVIEW1",
  "kind": "tool",
  "phase": "tool",
  "provider": "claude",
  "lane": "wk-claude",
  "disposition": "rendered",
  "ts": "2026-08-13T12:00:00Z",
  "parent_source_event_id": "01J...",
  "payload": {},
  "integrity": {
    "authority": "wk",
    "tool_call_id": "tool-42",
    "mutation": false,
    "exit_code": null,
    "status_write_seq": 17
  }
}
```

Required fields:

| Field | Rule |
| --- | --- |
| `schema` | Exact version string. Unknown versions are `unknown` and do not enter the visible view. |
| `source_event_id` | Unique ID from the core. It never changes on a tool update. |
| `source_seq` | Monotonic per-run source sequence. The core assigns it before publication. |
| `run_id`, `agent_id` | Match the supervisor run record. |
| `kind` | One of the existing visible kinds, plus `run`, `turn`, `tool`, `status`, `integrity`, `compaction`, and `error` for harness events. |
| `phase` | `run`, `turn`, `assistant`, `tool`, `status`, `compaction`, or `archive`. |
| `provider`, `lane` | `claude` or `codex`, and `wk-claude` or `wk-codex`. |
| `disposition` | `rendered`, `summarized`, `intentionally_ignored`, or `unknown`. |
| `ts` | Provider time when trustworthy, otherwise core receive time. |
| `parent_source_event_id` | Causal parent for tool, result, status, and compaction events. |
| `payload` | Typed provider-neutral payload. Redact secrets and unbounded output. |
| `integrity` | Harness facts. It is absent only for provider-only diagnostic events. |

### 4.2 Typed payloads

The v0 payload types are:

- `run.started`, `run.resumed`, `run.completed`, `run.blocked`, `run.archived`.
- `assistant.message` with text, thinking summary, stop reason, and usage.
- `tool.started` with tool name, validated input, argument hash, and mutation class.
- `tool.updated` with bounded progress output.
- `tool.completed` with result, success, exit code, duration, output hash, and mutation receipt.
- `tool.failed` with stable error class and bounded detail.
- `status.changed` with old and new status, step, blocker, and status-write sequence.
- `compaction.started` and `compaction.completed` with cut point, retained range, summary hash, and token estimates.
- `approval.requested`, `approval.resolved`, and `error`.

Tool completion updates the same logical event ID as tool start. The materializer writes a patch for the update. This matches the existing stable event and patch contract (`backend/app/transcripts.py:3955-3997`) and WIKI-282's `events` plus `patches` tables (`../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md:46-91`).

### 4.3 WIKI-282 mapping

| WIKI-282 field | `wk` source |
| --- | --- |
| `raw_seq` | `source_seq` in the durable raw envelope |
| `event_id` | Materializer-assigned stable UI ID |
| `kind` | Envelope `kind` |
| `event_json` | Complete envelope, including `integrity` |
| `created_at` | Envelope `ts` |
| `updated_at` | Materializer update time |
| `revision` | Incremented for tool, status, and projection updates |
| `deleted` | Tombstone only when an existing view rule removes a row |
| `dispositions` | One row per source sequence |

The core emits events into the existing raw-before-normalized boundary. The supervisor must append raw, materialize the event, commit the SQLite transaction, and publish invalidation in that order. WIKI-282 specifies this sequence and crash replay from raw JSONL (`../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md:176-226`).

## 5. Integrity tool layer

Integrity is a harness boundary, not a prompt instruction. The model can request a tool. It cannot write a result, a status file, or a gate verdict.

### 5.1 Tool set

| Tool | Behavior | Integrity proof |
| --- | --- | --- |
| `wk.read` | Read a bounded file or directory listing under the run's allowed roots | Path resolution and byte count |
| `wk.write` | Write one file through the harness mutation queue | Before hash, after hash, byte count, and mutation receipt |
| `wk.edit` | Apply an exact or bounded diff | Before hash, matched ranges, after hash, and diff hash |
| `wk.bash` | Run a command with cwd, environment, timeout, stdout, stderr, and process identity capture | Real exit code, signal, duration, output hashes, and mutation class |
| `wk.gate` | Run `wiki gate <pr> --json` with an optional expected SHA | Real exit code plus parsed JSON verdict and raw output hash |
| `wk.status` | Request a status transition through the core | Core-written atomic status file and status event |
| `wk.archive` | Ask the supervisor to archive after the core reaches a terminal state | Archive manifest and completion marker |

`wk.bash` never returns a successful result without the real subprocess result. A non-zero exit is a tool error. A timeout records the timeout and process termination. The tool may return bounded output while retaining a full output artifact path.

The existing composer gate already runs `wiki gate --json`, captures stdout and stderr, checks the process return code, and rejects missing or unusable verdicts (`backend/app/main.py:5870-5917`). `wk.gate` moves this behavior into the worker tool boundary. It must retain the exact exit code even when the JSON says `ready: true`.

### 5.2 Mutation ledger

Every `write`, `edit`, and `bash` call creates a durable ledger pair:

1. `tool.started` records the validated arguments, path set, mutation class, and input hash.
2. The process or file operation runs under the harness.
3. `tool.completed` or `tool.failed` records the real result.
4. The core records a mutation receipt with before and after fingerprints.

Mutation class is one of `none`, `file`, `git`, `process`, or `unknown`. `unknown` is treated as mutating for policy and reporting. The first v0 policy permits `unknown` only for a reviewed `bash` command and records it as a risk.

The ledger is append-only. A crash after `tool.started` leaves an open operation. Recovery marks it `aborted` unless the filesystem fingerprint proves a completed mutation. It never changes an open operation to success from model text.

### 5.3 Gate and finish rules

The core may write `merge-ready` only when all applicable checks pass:

- The final `wk.gate` result has a recorded process exit code and matching expected SHA.
- `wiki lint` passes for a planning worker.
- No mutation operation is open.
- The final status write succeeds atomically.
- The supervisor command receipt is durable.

The core writes `blocked` when a provider, tool, status write, or recovery operation fails. A model message such as `MERGE-READY` is transcript text only. It is not a status transition.

### 5.4 Status ownership

The current runtime card tells workers to atomically rewrite `/tmp/agent-status/<ticket>.json` on each transition and before long operations (`backend/app/agent_runtime/runtime_card.py:55-82`). `wk` turns that instruction into an enforced core operation:

- The loop writes the status file before a long provider call, tool call, gate, compaction, or archive.
- The loop writes the status file after each operation.
- The loop uses temp-file plus rename and validates the JSON shape.
- The model has no file-tool route to the status path.
- The status event includes a monotonically increasing `status_write_seq`.
- The supervisor reads the file for parity, but the loop is the writer for wk runs.

This makes status claims physically auditable. It also preserves the existing supervisor status-file path and archive handoff behavior.

## 6. Feature-flag surface

Use one process-start environment flag: `WIKI_ENABLE_WK=1`. The default is off.

### Off behavior

- `wk_enabled` resolves to false once at backend and supervisor startup.
- Spawn validation accepts only the current `cc` and `cdx` kinds.
- `kind=wk` returns the current closed-kind validation error.
- Model catalogs, UI options, runtime cards, and CLI help omit `wk`.
- The supervisor does not import, construct, or start a wk adapter.
- No wk event, session, status, or experiment code runs.
- Existing `cc` and `cdx` requests use the current code paths and payloads.

This is the byte-identical default contract. A flag check must not change current responses, registry shapes, event order, or provider commands when the flag is absent.

### On behavior

When enabled, `SpawnWorkerIn` accepts `kind=wk` only for `role=review` in v0. Add a required `lane` field for wk requests with values `claude` or `codex`. Reject `lane` for `cc` and `cdx`. `SpawnOrchestratorIn` continues to reject `wk` in v0.

The supervisor receives `execution_kind=wk` and `wk_lane=<lane>` as explicit run metadata. Do not overload `ProviderKind`; the provider is still needed for health, auth, and event normalization. The registry exposes `kind=wk`, `lane=claude|codex`, and the same run ID and lifecycle fields as other headless runs.

### Rollback

Set `WIKI_ENABLE_WK=0` and restart the backend and supervisor. Existing wk runs finish under their recorded adapter. New wk runs are rejected. A restart with the flag off must recover or block an existing wk run without relaunching it through `cc` or `cdx`.

## 7. Supervisor integration and parity

### Spawn

Add a single admission check before current kind validation. It rejects wk when disabled and rejects non-review roles in v0. The route then calls the same idempotent `run/start` command. The supervisor creates a run record, injects a runtime card, persists the record, constructs the selected wk lane, and commits the start. The current transaction and idempotency behavior stays in place (`backend/app/agent_runtime/supervisor.py:2421-2466`, `backend/app/agent_runtime/supervisor.py:2475-2534`).

The adapter factory dispatches `wk + claude` to the Claude SDK lane and `wk + codex` to the App Server lane. It must not construct either adapter when the flag is off.

### Steer and queue

Keep `/api/agents/{ticket}/message` and the supervisor's `run/send_now` and `run/send_on_idle` operations. The wk core maps them to its steering and follow-up queues. Preserve request IDs, pending IDs, dedupe keys, and command receipts. The event ledger records queue enqueue, delivery proof, and failure.

### Status and events

The existing `/api/agents` list reads the registry and status files. Add wk metadata without changing the shape for cc or cdx. `runtime_state`, provider state, status-file state, and core phase must be visible for wk. A mismatch is a blocked integrity condition, not a model finding.

The supervisor continues to publish the same session invalidation after durable event ingest (`backend/app/agent_runtime/supervisor.py:1161-1179`). WIKI-282's SQLite materializer becomes the next ingest consumer. The UI still reads the version 2 session response during migration.

### Resume and replacement

Resume restores the wk session ID, session cursor, open operation records, steering queue, and status file. An open tool operation is recovered as aborted unless filesystem proof says otherwise.

Replace creates a new run with `replaces_run_id` and a parent session reference. It does not reuse a live provider transport. The old run receives a terminal replacement event. The new run starts with a clean status sequence and a session-tree parent link.

### Archive

Archive first drains the core, writes the final status, flushes the event sink, and closes the provider. It then uses the existing archive commit boundary. The JSONL archive remains portable. If WIKI-282 has a materialized view, export that view before the archive completion marker. WIKI-282 requires the completion marker to remain the visibility gate (`../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md:226`, `../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md:302`).

## 8. Pi patterns studied

The required read-only study used `earendil-works/pi` at commit `6f707eb36064e82af9c1320a7634f4dfad21049b`, cloned to `/tmp/wiki-289-pi`. The links below point to that snapshot.

### Adopt

| Pattern | Pi reference | wk use |
| --- | --- | --- |
| Explicit outer and inner loop | [`agent-loop.ts:155-274`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/agent-loop.ts#L155-L274) | Separate provider turns from tool-call continuation and follow-up messages. |
| Typed tool preparation and hooks | [`agent-loop.ts:600-755`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/agent-loop.ts#L600-L755) and [`types.ts:80-99`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/types.ts#L80-L99) | Validate tool input before execution. Record before and after hooks around every tool. |
| Real exit-code handling | [`bash.ts:109-159`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/tools/bash.ts#L109-L159) | Preserve exit codes, timeout errors, bounded output, and full-output references. Add Wiki mutation receipts. |
| Single-writer queued session writes | [`storage.ts:48-52`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/session/jsonl/storage.ts#L48-L52) and [`storage.ts:258-275`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/session/jsonl/storage.ts#L258-L275) | Serialize event, session, and status publication per run. |
| Parent-linked session tree | [`session/types.ts:14-74`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/session/types.ts#L14-L74) and [`session/types.ts:328-372`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/session/types.ts#L328-L372) | Keep replacement, compaction, and future branch history queryable without rewriting raw events. |
| Atomic publication | [`storage.ts:23-45`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/session/jsonl/storage.ts#L23-L45) | Use temp-file plus rename for status and staged derived artifacts. |
| Structured compaction | [`compaction.ts:374-422`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/compaction/compaction.ts#L374-L422) and [`compaction.ts:615-686`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/compaction/compaction.ts#L615-L686) | Select a valid cut point, retain a recent tail, record a summary, and preserve file-operation context. |
| Branch summaries | [`branch-summarization.ts:82-110`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/agent/src/harness/compaction/branch-summarization.ts#L82-L110) | Record what a replaced or exploratory lane did before returning to a parent. |
| Versioned RPC handshake and snapshots | [`server.ts:170-269`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/server/src/server.ts#L170-L269) and [`schemas.ts:233-375`](https://github.com/earendil-works/pi/blob/6f707eb36064e82af9c1320a7634f4dfad21049b/packages/protocol/src/schemas.ts#L233-L375) | Apply request IDs, explicit errors, snapshots, and version checks at the provider boundary. |

### Reject or defer

- Reject Pi's provider-neutral AI transport as a new billing path. `wk` must use the Claude Code plan runtime and Codex App Server plan auth.
- Reject Pi's generic session server as a second Wiki control plane. Supervisor HTTP and its command journal remain authoritative.
- Reject free-form provider tools. Pi's typed tool shape is useful, but Wiki adds path policy, mutation receipts, real exit codes, and status ownership.
- Defer user-visible branch navigation. The session tree is an internal v0 record. Expose it only after reviewer experiments show a need.
- Defer full cross-lane transcript replay. Store raw provider payloads for audit, but normalize visible events through one reducer.
- Do not copy Pi code. The implementation uses the patterns and file boundaries above, not source code.

## 9. Reviewer-lane experiment

### Protocol

Run a two-week reviewer-only experiment after the harness passes recovery and parity tests.

1. Keep `cc` and `cdx` available and unchanged.
2. Select review tickets with a pinned PR SHA and a fixed reviewer prompt.
3. Pair each eligible review with `cdx` sol and one wk lane. Randomize lane order.
4. Keep the implementer, PR, SHA, prompt, role, and review lens matched.
5. Blind the quality adjudicator to lane identity.
6. Record every finding, severity, file, line, acceptance outcome, and follow-up fix.
7. Stop the experiment on any false-ready, lost mutation, missing gate exit code, or unrecoverable status mismatch.
8. Keep wk limited to review workers. Do not use it for plan, implement, or orchestrator runs.

Start with a balanced sample. The exact sample size and lane split are open questions below. Include both `wk-claude` and `wk-codex` only if each lane passes the same integrity gate.

### Metrics

| Area | Metric | Source |
| --- | --- | --- |
| Quality | Severity-weighted finding recall, precision, and accepted-finding rate | Blind adjudication and merged follow-up diffs |
| Quality | Escaped defects found after review | Follow-up CI, later review, and issue audit |
| Quality | First-pass clean rate and rounds to clean | Gate and review history |
| Speed | Time to first finding, time to verdict, and time to clean | Supervisor timestamps |
| Cost | Provider-reported input/output tokens when available, tool calls, retries, wall time, and process time | Event store and provider usage |
| Reliability | Run completion, restart recovery, approval recovery, and archive success | Lifecycle and recovery events |
| Integrity | Gate exit-code completeness, mutation receipt completeness, status-write completeness, and false-ready count | Harness event ledger |
| Operator load | Manual interventions, blocked runs, and reruns | Command journal and adjudication log |

Compare medians and p95 values. Report confidence intervals where the sample supports them. Do not convert plan usage into dollars unless the provider exposes a stable plan-unit measure. Report wall time and usage units separately.

### Expansion gate

Expand beyond reviewers only when:

- wk has zero false-ready results and zero unaccounted mutations.
- Gate exit codes and status writes are complete for every run.
- Quality is non-inferior to cdx sol on the agreed severity-weighted measure.
- Recovery and archive tests pass on the experiment data.
- The measured cost and operator load are acceptable to Henry.

cc and cdx stay first-class after expansion. wk is an additional execution kind, not a replacement.

## 10. Phased PR-sized tickets

### PR 1: define wk core and event contract

Add the feature-flag helper, run metadata for `kind=wk` and `lane`, shared event envelope, core interfaces, typed tool contract, and status writer. Add unit tests for off-by-default behavior, sequence assignment, redaction, and atomic status writes. Do not start providers.

### PR 2: add the Claude Agent SDK lane

Add the plan-auth SDK adapter with default tools disabled, Wiki-owned read/write/edit/bash/gate/status tools, turn and steering loop, tool event ledger, and provider event translation. Add fixture tests for tool success, non-zero exit, timeout, approval, interruption, and resume.

### PR 3: add the Codex App Server lane

Add the thin App Server driver, request and server-request matching, tool-policy negotiation, lifecycle mapping, and shared event translation. Reuse the protocol behavior already represented in `backend/app/agent_runtime/codex.py` and `backend/app/transcripts.py`. Block the lane when the tool policy cannot prove harness ownership.

### PR 4: integrate supervisor parity and WIKI-282 ingest

Add wk admission, adapter-factory dispatch, start/resume/steer/replace/archive/status parity, command receipts, recovery of open operations, and raw-before-normalized event ingest. Feed the same event envelope into the WIKI-282 materializer while retaining JSONL dual-write. Add route and registry parity tests.

### PR 5: add integrity and experiment telemetry

Add the gate runner, mutation receipt reconciliation, false-ready guard, experiment allowlist, lane randomization, matched-review identifiers, and metric export. Add a replay test that proves a model cannot create a successful gate or status event without a real tool result.

### PR 6: run and review the two-week experiment

Run the reviewer cohort, adjudicate quality, publish the cost and reliability report, and make the expansion decision. Keep the flag off by default until the report meets the expansion gate.

## 11. Risks and controls

| Risk | Control |
| --- | --- |
| Plan-auth runtime falls back to API-key auth | Startup capability check. Block the run. Do not retry through another billing path. |
| Codex App Server executes a provider-native tool outside Wiki | Require tool-policy proof. Keep wk-codex disabled until proof exists. |
| A provider emits a tool result without a harness execution | Treat it as an audit-only provider event. Do not mark the tool complete. |
| Gate JSON and process exit disagree | Store both. Use the documented gate contract. Never let model text decide. |
| Crash leaves a file mutation half-recorded | Reconcile before/after fingerprints and mark ambiguous operations blocked. |
| Status file is stale or model-authored | Only the core writes it. Record each write sequence and validate before final status. |
| New loop changes current cc/cdx behavior | Keep wk behind startup flag and conditional construction. Add byte-compatibility tests with the flag off. |
| SQLite view and JSONL diverge | Preserve raw JSONL, replay by source sequence, and run WIKI-282 parity checks before route flips. |
| Compaction loses operational context | Keep raw events, retain the recent tail, include file operations in the summary, and emit compaction events. |
| Experiment selection favors one lane | Match PR SHA and prompt. Randomize order. Blind adjudication. |
| Reviewer quality looks good but costs more operator time | Track manual interventions and reruns as first-class metrics. |

## 12. Open questions for Henry

1. Should `kind=wk` use a body field named `lane`, or should the API expose `wk-claude` and `wk-codex` as separate kinds?
2. What minimum sample size and severity-weighted non-inferiority margin should end the two-week reviewer experiment?
3. Should wk-codex be in the first experiment, or should wk-claude prove the tool and status boundary first?
4. Which plan-usage measure is available and acceptable for the cost report when no API price exists?
5. Should `merge-ready` require a clean working tree, or only the exact gate and lint checks for the worker role?
6. Should compaction and session-tree records enter the WIKI-282 visible event view in v0, or remain audit-only until the UI needs them?
7. Should committed archives include a verified SQLite snapshot, or keep the WIKI-282 recommendation of raw and exported JSONL first?

## References

- Current supervisor and provider code: `backend/app/agent_runtime/supervisor.py`, `backend/app/agent_runtime/factory.py`, `backend/app/agent_runtime/provider.py`, `backend/app/agent_runtime/claude.py`, `backend/app/agent_runtime/codex.py`.
- Current normalized event model: `backend/app/agent_runtime/normalizer.py`, `backend/app/agent_runtime/store.py`.
- Current transcript IDs, patches, cursors, and cache: `backend/app/transcripts.py`.
- WIKI-282 normalized event store: `../wiki-282-design/vault/wiki/specs/wiki-282-normalized-event-store.md`, commit `5116712f88bf4c5e90f3c08621afeec520e11cfa`.
- Pi study snapshot: `/tmp/wiki-289-pi`, commit `6f707eb36064e82af9c1320a7634f4dfad21049b`.
