---
type: decision
tags: [wiki-app, wk, architecture]
created: 2026-08-19
updated: 2026-08-19
---

# WIKI-361: wk-owned agent loop design

Produced by WIKI-361-PLAN (2026-08-19, cdx luna xhigh; pi prior-art pass steered in by Henry). Supersedes nothing; gates the TUI ladder revisions in [[wk-tui-design]].

## Decision summary

Move the durable agent loop into `wk`. The provider processes become plan-authenticated completion transports. They must not be the source of truth for conversation state, tool execution, context assembly, approvals, or resume.

Keep the existing `cc` and `cdx` execution paths unchanged. Put the new loop behind a second wk flag. Run old and new wk paths in parallel until fixture, live-lane, restart, approval, and TUI parity passes.

Use these provider seams as provisional choices. Each must pass a single-completion boundary test.

- Claude: headless Claude CLI `stream-json` with a wk-owned MCP bridge. The CLI supplies the plan-authenticated model session and stream. Wk owns the MCP server, tool registry, canonical messages, and outer turn loop if the CLI exposes tool calls before execution.
- Codex: Codex App Server stdio with `account/read`, `thread/start`, `turn/start`, and dynamic Wiki tools. Wk owns the same state and loop if the App Server boundary permits one completion before the next tool result. App Server threads are transport handles, not the durable conversation.

Do not use a raw provider API. Do not make an undocumented SDK or `exec/proto` mode the primary seam. Keep both as later experiments. If either chosen transport still owns the whole tool loop, WIKI-363 or WIKI-364 must fail closed and Henry must choose between relaxing the raw-API ban or accepting provider-inner-loop behavior.

## Scope and non-goals

In scope:

- One provider-neutral conversation model for Claude and Codex.
- Wk-owned context assembly, compaction, tool dispatch, approvals, streaming, and resume.
- Durable session state beside the existing run and event files.
- Normalizer-compatible events for `render.py`, the supervisor, and the TUI.
- A staged migration with the current provider loops available behind a flag.

Out of scope:

- Decrypting Codex private reasoning. The current product only receives reasoning summaries; the design keeps that limit.
- Replacing the headless supervisor, RunStore, SQLite materializer, or archive protocol.
- Changing the legacy `cc` or `cdx` run format during the first migration stages.
- Adding a second tool registry for the TUI or a provider.

## Current state map

The current system has two layers. `claude.py` and `codex.py` are the existing headless provider adapters. `wk_claude.py` and `wk_codex.py` are the opt-in wk lanes. The supervisor owns durable run files and consumes adapter events.

| Responsibility | Claude today | Codex today | What wk already owns | Target owner |
|---|---|---|---|---|
| Process and turn lifecycle | `ClaudeStreamAdapter` starts a persistent CLI process, sends user messages, interrupts, replaces, archives, and resumes by session ID (`backend/app/agent_runtime/claude.py:134-185`, `:737-920`). | `CodexAppServerAdapter` owns the stdio process, JSON-RPC requests, thread, turn, steer, interrupt, replace, and archive (`backend/app/agent_runtime/codex.py:70-130`, `:736-1020`). | `WkClaudeLane` and `WkCodexLane` add start/send/resume/interrupt/replace calls around those transports (`backend/app/agent_runtime/wk_claude.py:1335-1449`, `backend/app/agent_runtime/wk_codex.py:600-731`). | `WkLoop` owns turn state. A thin backend owns only provider process I/O and one completion turn. |
| Conversation state | Claude Code owns the active conversation and writes a provider transcript. Wk records provider frames, but does not build a message list. | App Server owns the thread history and reports a transcript path. Wk records frames and a parent-linked event tree. | `WkSessionTree` stores redacted event envelopes in memory (`backend/app/agent_runtime/wk_common.py:34-70`). `WkProviderAdapter` restores it from `raw.jsonl` and `events.jsonl` (`backend/app/agent_runtime/wk_adapter.py:39-47`, `:91-107`). | `WkConversationStore` owns typed user, assistant, tool-call, tool-result, approval, and compaction messages. Provider history is a cache. |
| Context assembly | The CLI/SDK chooses the provider context. The legacy adapter only passes text and a session ID (`backend/app/agent_runtime/claude.py:370-387`, `:754-780`). | `thread/start` and `thread/resume` select App Server history. `historyMode="legacy"` is provider-side (`backend/app/agent_runtime/codex.py:736-755`). | No context assembler exists. The `WkSessionTree` is an event audit tree, not a model prompt (`backend/app/agent_runtime/wk_common.py:34-70`). | `WkContextAssembler` builds every completion request from the canonical message store and tool schemas. |
| Compaction | Claude emits compaction-related system events. The provider decides when and how to compact. The translator only maps a compact subtype to `WkEventPhase.COMPACTION` (`backend/app/agent_runtime/wk_claude.py:345-366`). | App Server emits `context/compacted`; the normalizer renders it, but no wk policy acts on it (`backend/app/agent_runtime/wk_codex.py:272-297`, `backend/app/agent_runtime/normalizer.py:18-39`). | Wk records the event phase, but has no token budget or summary policy. | `WkCompactionPolicy` decides when to compact, preserves open operations, persists the summary, and treats provider compaction as telemetry only. |
| Tool registry and execution | The current SDK lane exposes only Wiki MCP tools, but the SDK invokes the handler inside its own turn loop (`backend/app/agent_runtime/wk_claude.py:980-1066`, `:1235-1249`). The file contains an older duplicate tool implementation (`backend/app/agent_runtime/wk_claude.py:528-831`). | The lane registers a Wiki dynamic-tool namespace and rejects native tools. Dynamic calls go through `WkToolBridge` (`backend/app/agent_runtime/wk_codex.py:43-58`, `:420-425`, `:485-508`, `:566-585`). | `WkToolRegistry` validates and dispatches typed tools (`backend/app/agent_runtime/wk_core.py:372-415`). `wk_tools.py` contains the provider-neutral implementations (`backend/app/agent_runtime/wk_tools.py:17-25`, `:49-433`, `:477-549`). `WkToolLedger` proves start/result pairing (`backend/app/agent_runtime/wk_common.py:98-333`). | One registry and one dispatcher in wk. Claude MCP names and Codex dynamic names translate into the same `WkToolRequest`. Provider code never executes a file or process tool. |
| Approvals | The legacy adapter tracks Claude control requests, AskUserQuestion state, generations, and response IDs (`backend/app/agent_runtime/claude.py:403-583`, `:922-983`). The SDK lane uses `can_use_tool` and an optional callback (`backend/app/agent_runtime/wk_claude.py:1019-1028`). | The adapter tracks App Server request IDs and approval methods (`backend/app/agent_runtime/codex.py:55-67`, `:370-417`, `:1022-1049`). The wk lane responds to provider dynamic calls, but provider approvals remain transport-owned (`backend/app/agent_runtime/wk_codex.py:694-701`). | The supervisor normalizer creates durable `pending_requests` and clears them on resolution (`backend/app/agent_runtime/store.py:278-308`). Lifecycle states include `WAITING_APPROVAL` (`backend/app/agent_runtime/types.py:23-38`). | `WkApprovalPolicy` decides allow, deny, or ask. The provider backend only carries the response. Pending requests persist before publication to the UI. |
| Streaming | The CLI adapter reads newline-delimited JSON, emits stdout, stderr, control, and process events, and maintains generation state (`backend/app/agent_runtime/claude.py:585-673`). | The App Server adapter reads JSON-RPC frames, maps state, and emits server, client, stderr, and process events (`backend/app/agent_runtime/codex.py:475-566`). | Translators produce `WkEventEnvelope` values and retain raw frames (`backend/app/agent_runtime/wk_claude.py:374-502`, `backend/app/agent_runtime/wk_codex.py:252-361`). The supervisor writes raw before normalized events (`backend/app/agent_runtime/supervisor.py:1241-1341`). | The backend emits raw frames plus the existing `normalizer.py` taxonomy. Wk-specific ledger and status events remain separate. `render.py` and the supervisor continue to consume raw and normalized shapes. |
| Session files and restart | Claude stores a provider session ID and discovers the native transcript path under the Claude config directory (`backend/app/agent_runtime/claude.py:705-735`). | Codex stores a thread ID and provider transcript path returned by App Server (`backend/app/agent_runtime/codex.py:598-650`). | RunStore persists `run.json`, `raw.jsonl`, `events.jsonl`, and provider metadata (`backend/app/agent_runtime/store.py:1368-1387`, `:2586-2714`). Wk archives the same files but not the canonical message list (`backend/app/agent_runtime/store.py:1389-1457`). | Add a versioned wk conversation log and compaction snapshot to each run. Archive them with the existing files. Resume rebuilds wk state first, then reattaches or recreates a provider transport. |
| Status and merge integrity | Provider wrappers do not own wk status. | Provider wrappers do not own wk status. | `WkLoop` owns atomic status writes, checksums, merge-ready validation, and revocation (`backend/app/agent_runtime/wk_core.py:467-745`). The ledger proves mutation completion and gate receipts (`backend/app/agent_runtime/wk_common.py:332-433`). | Keep this ownership. The loop calls the same status and ledger boundary after every tool result. |
| Durable supervisor projection | Supervisor owns RunRecord, raw JSONL, normalized JSONL, SQLite materialization, lifecycle state, pending requests, and archive (`backend/app/agent_runtime/supervisor.py:628-700`, `:1211-1466`; `backend/app/agent_runtime/store.py:2586-2714`). | Same path. | Wk events are already dual-written into the supervisor projection (`backend/app/agent_runtime/store.py:80-180`). | Keep the supervisor as the durable run and event owner. Do not move fleet locks or archive cleanup into the provider backend. |

### The main gap

Wk currently owns the typed tool boundary, but not the agent loop. `WkClaudeLane._query_provider()` calls SDK `query()`, and `WkCodexLane` starts provider turns through App Server (`backend/app/agent_runtime/wk_claude.py:1270-1282`, `backend/app/agent_runtime/wk_codex.py:600-618`). Both providers can still choose context, compaction, and continuation behavior.

`WkSessionTree` and the ledger are useful foundations. They are not enough for the target. The design must add a canonical message store and a loop that decides when a model turn ends, when a tool runs, when an approval blocks, and when context compacts.

## Plan-authenticated backend seam

### Claude decision: headless CLI stream-json plus a wk MCP bridge

The current legacy adapter proves that Claude CLI has the needed transport shape. It runs `claude -p` with `--output-format stream-json`, `--input-format stream-json`, partial messages, summarized thinking, stdio permission prompts, MCP config, and an explicit session ID or resume ID (`backend/app/agent_runtime/claude.py:229-261`). It can send a user frame and receive a stream without an HTTP provider client (`backend/app/agent_runtime/claude.py:370-387`, `:585-673`).

The current wk lane proves the plan-auth and tool-policy checks. It allowlists the process environment, rejects API credentials, checks `auth status --json`, requires a first-party subscription identity, requires exactly the Wiki MCP tools, and rejects hooks and settings sources (`backend/app/agent_runtime/wk_claude.py:116-201`, `:434-468`). Its SDK options also show the required policy shape: no built-in provider tools, strict MCP configuration, no hooks, no settings, and a Wiki system prompt (`backend/app/agent_runtime/wk_claude.py:1030-1066`).

The target backend will use those checks with the CLI transport. Wk will start one provider completion session with only the Wiki MCP bridge enabled. The bridge maps `mcp__wiki__read` and related names to `wk.read` and related names. The actual registry call occurs in wk. Native Claude tools are disabled or rejected at startup. An unexpected provider-native tool is a policy error.

The provider session ID is a transport handle. Wk rehydrates the full canonical context for a new turn. This avoids making the provider transcript the source of truth. It gives up provider-side conversation caching and makes prompt assembly more expensive, but it makes restart and compaction deterministic.

#### Candidate evaluation

| Candidate | Evidence | Keeps | Gives up or risks | Decision |
|---|---|---|---|---|
| Claude CLI headless `stream-json` | Exact command and bidirectional frame handling exist in `claude.py:229-387`. | Plan login, streamed assistant and tool frames, control responses, session IDs, summarized thinking, and native process isolation. | CLI flags and MCP behavior can drift. The CLI may compact its transient session. Wk must reject native tools and rehydrate context. Provider-side cache is not reusable. | **Provisional pick.** Add a single-completion boundary test before enabling the flag. |
| Agent SDK “bare completion” | The local protocol exposes `connect`, `query`, `interrupt`, and `receive_messages`, not a completion-only method (`wk_claude.py:104-113`). The lane waits for a provider `result` and the SDK invokes MCP handlers during that loop (`wk_claude.py:1235-1249`). | Typed SDK messages, strict MCP, plan-auth checks, and current fixture coverage. | No evidence of a bare-completion mode. SDK context, compaction, and tool-loop behavior remain authoritative. | **Reject as primary.** Keep the current SDK lane as the rollback path until CLI parity passes. |
| Raw Anthropic API | No provider API client exists in this design, and it would need API credentials or a new auth bridge. | Direct completion and explicit tool calls. | Violates the plan-auth-only and no-raw-provider-API requirements. | **Reject.** |

The direct CLI bridge is a migration contract, not an assumption. WIKI-363 must prove all of these before it can replace the SDK lane: exact plan-auth proof, no native tool call, one complete assistant result, tool request and result round trip, partial stream order, interrupt, and clean process exit. Failure blocks the new Claude flag and leaves the old wk SDK lane active.

### Codex decision: App Server stdio with disposable dynamic-tool threads

The current App Server adapter already has a narrow, plan-authenticated channel. It starts `codex app-server --stdio`, performs `initialize`, and uses JSON-RPC over stdin/stdout (`backend/app/agent_runtime/codex.py:215-275`). It proves account identity on the same connection with `account/read` and requires a ChatGPT plan account (`backend/app/agent_runtime/wk_codex.py:544-565`, `backend/app/agent_runtime/codex.py:764-768`). The environment and TOML checks reject API credentials and credential-bearing settings (`backend/app/agent_runtime/wk_codex.py:106-177`).

The wk lane already registers one `wiki` dynamic-tool namespace, sets native sandbox restrictions, requires `approvalPolicy: never`, rejects native tool items, and dispatches dynamic calls through `WkToolBridge` (`backend/app/agent_runtime/wk_codex.py:43-58`, `:438-450`, `:485-542`, `:566-585`). The target keeps that wire shape. Wk owns the policy and registry. App Server only transports the call and result.

Wk will treat `thread/start`, `turn/start`, `turn/steer`, and `thread/resume` as transport operations. A new user turn receives an assembled context. The provider thread is not the canonical history. Wk may use `thread/resume` to recover an in-flight provider turn, but a normal application resume reconstructs the context from the wk session log.

#### Candidate evaluation

| Candidate | Evidence | Keeps | Gives up or risks | Decision |
|---|---|---|---|---|
| Codex App Server methods | Current adapter implements initialize, account/read, thread/start, thread/resume, turn/start, turn/steer, turn/interrupt, approval responses, raw events, and transcript identity (`codex.py:249-355`, `:736-868`, `:1022-1049`). | Plan identity, streamed protocol frames, dynamic tools, approvals, interrupts, and exact current fixture coverage. | App Server has its own thread history and can emit `context/compacted`. Wk must treat that state as disposable. Starting fresh threads gives up provider cache and may increase latency. | **Provisional pick.** This is the only proven Codex seam in the repository, but the loop boundary is unproven. |
| Codex `exec/proto` | No implementation, fixture, auth proof, approval mapping, or resume contract exists in this repository. | It may provide a smaller one-shot completion protocol. | Unknown plan-auth proof, tool wire, streaming, interruption, session identity, and compaction behavior. | **Reject for the first migration.** Add a separate probe only after the App Server loop is stable. |
| Raw OpenAI API | Not part of the current adapter. | Direct completion and explicit tool calls. | Violates the plan-auth-only and no-raw-provider-API requirements. | **Reject.** |

Provider compaction events remain visible telemetry. `normalizer.py` already classifies `context/compacted` as rendered and maps token usage to summarized events (`backend/app/agent_runtime/normalizer.py:18-57`). The wk compaction policy must not use a provider event to rewrite its canonical messages.

## Prior art: pi

### Scope and provenance

Pi was cloned read-only from `https://github.com/earendil-works/pi` into `/tmp/wiki-361-pi-ref` at commit `496185f6e4267b979e3663c45f7eb70b0c6a97b4`. The repository is MIT licensed (`pi/LICENSE:1-13`, copyright Mario Zechner). This plan copies design ideas and cites source files. It does not vendor pi code. Any future code copy needs the MIT notice and a provenance review.

Pi has two useful layers. `@earendil-works/pi-agent-core` supplies a provider-neutral loop. `packages/coding-agent` adds durable sessions, compaction, tools, extensions, and the interactive host. This separation is closer to the wk target than either current provider wrapper.

### Provider seam and plan authentication

Pi uses direct provider protocols, not Claude CLI, Agent SDK, or Codex App Server:

- Claude is an Anthropic Messages API provider at `https://api.anthropic.com`. `anthropicProvider()` marks Claude Pro/Max OAuth as a subscription and selects the Anthropic stream implementation (`pi/packages/ai/src/providers/anthropic.ts:43-58`). Its OAuth flow stores refresh and access tokens, then derives request auth from the access token (`pi/packages/ai/src/auth/oauth/anthropic.ts:355-364`). The stream uses the SDK's Messages API request and parses message, thinking, text, and tool-use blocks into a provider-neutral stream (`pi/packages/ai/src/api/anthropic-messages.ts:570-584`, `:589-729`).
- Codex is an OpenAI Responses-style provider at `https://chatgpt.com/backend-api`. `openaiCodexProvider()` marks ChatGPT Plus/Pro OAuth as a subscription (`pi/packages/ai/src/providers/openai-codex.ts:7-21`). Its OAuth flow extracts the ChatGPT account ID from the access token and refreshes it under the OAuth store (`pi/packages/ai/src/auth/oauth/openai-codex.ts:396-415`, `:508-544`). The stream sends a direct WebSocket or SSE request, includes prompt-cache identity, and maps Responses events into the common stream (`pi/packages/ai/src/api/openai-codex-responses.ts:230-313`, `:368-489`, `:519-591`, `:652-665`).
- Auth is subscription-aware but still API-shaped at the final call. `resolveProviderAuth()` refreshes OAuth credentials under a per-provider serialized store (`pi/packages/ai/src/auth/resolve.ts:44-61`, `:122-179`). `Models.applyAuth()` turns the resolved OAuth token into `apiKey`, headers, and provider options before calling the provider stream (`pi/packages/ai/src/models.ts:636-695`). This avoids asking the user for a raw key, but it is still a new raw provider API integration inside the harness.

Pi therefore proves a useful seam shape: one request receives assistant deltas and tool-call deltas; the harness runs the tool; the next request carries the tool result. It also preserves provider-native cache identity, thinking blocks, usage, and wire details. It does not prove that the current wk CLI or App Server channels expose the same boundary.

### Loop, context, tools, and approvals

Pi keeps the loop provider-neutral. `runLoop()` owns turns, steering, follow-ups, tool calls, tool results, and stop conditions (`pi/packages/agent/src/agent-loop.ts:155-275`). Before each provider call it transforms the agent message list, converts it to LLM messages, builds the provider context, resolves expiring auth, and invokes one `streamFunction` (`pi/packages/agent/src/agent-loop.ts:281-312`). The provider stream only returns message events. The loop appends the assistant message, executes tools, appends tool results, and starts the next turn (`pi/packages/agent/src/agent-loop.ts:192-224`).

The tool registry is shared across providers. `AgentContext` carries one tool list, and `AgentTool` defines schema validation, execution, abort handling, and partial updates (`pi/packages/agent/src/types.ts:411-419`, `:385-409`). `executeToolCalls()` validates arguments, runs the configured pre-hook, executes sequentially or in parallel, and emits ordered results (`pi/packages/agent/src/agent-loop.ts:411-425`, `:600-700`). The coding host creates one standard set of tools rather than provider copies (`pi/packages/coding-agent/src/core/tools/index.ts:81-115`, `:138-195`). This supports the wk choice of one `wk_tools.py` registry.

Pi's approval boundary is weaker than wk's required policy. `beforeToolCall` can block a call and return an error tool result (`pi/packages/agent/src/types.ts:55-69`, `pi/packages/agent/src/agent-loop.ts:616-646`). Extension handlers can intercept `tool_call` and `tool_result` (`pi/packages/coding-agent/src/core/agent-session.ts:476-537`). The core event taxonomy has no durable approval event; its tool events are only start, update, and end (`pi/packages/agent/src/types.ts:428-443`). Project trust is a separate resource policy with a UI confirmation hook (`pi/packages/coding-agent/src/core/extensions/types.ts:519-541`).

Copy the separation between tool execution and provider translation. Do not copy the approval model. Wk must retain an explicit `approval` event, durable pending request, `WAITING_APPROVAL` lane state, stale-response checks, and provider response translation because current Claude and Codex wrappers already expose approval requests (`backend/app/agent_runtime/normalizer.py:58-66`, `:267-389`; `backend/app/agent_runtime/store.py:278-308`).

### Streaming event surface

Pi's stream is small and stable. The loop emits `agent_start`, `turn_start`, message lifecycle events, tool execution lifecycle events, `turn_end`, and `agent_end` (`pi/packages/agent/src/types.ts:421-443`). Provider-specific text, thinking, and tool-call deltas are nested inside `message_update`, so the UI does not parse provider frames. `Agent` reduces streaming state before awaiting listeners, and `agent_end` listeners are part of run settlement (`pi/packages/agent/src/agent.ts:537-591`).

Wk cannot replace its existing event surface with pi's names. The supervisor writes raw frames first, then normalizes and materializes them (`backend/app/agent_runtime/supervisor.py:1241-1466`). `normalizer.py` already defines the Claude and Codex taxonomy consumed by `render.py` and the supervisor (`backend/app/agent_runtime/normalizer.py:18-57`, `:164-209`, `:267-448`). Wk should copy pi's rule that the loop emits one provider-neutral stream, but each event must carry the existing raw payload, normalized taxonomy, Wk run/turn/generation metadata, and ledger identity. The TUI can then use pi-like lifecycle boundaries without breaking replay or SQLite projections.

### Session persistence and resume

Pi persists an append-only, parent-linked session tree. A session header carries version, ID, cwd, and parent session (`pi/packages/coding-agent/src/core/session-manager.ts:30-80`). Message, model-change, compaction, branch-summary, custom, and metadata entries share IDs and `parentId`; `SessionManager` appends them and advances the leaf (`pi/packages/coding-agent/src/core/session-manager.ts:1044-1119`). It rebuilds the active branch, then derives the LLM context separately (`pi/packages/coding-agent/src/core/session-manager.ts:1255-1303`, `:418-469`).

The lower-level harness makes the persistence contract explicit. JSONL storage validates the header, replays mutations, repairs a torn final line, serializes appends, and supports lanes (`pi/packages/agent/src/harness/session/jsonl/storage.ts:65-107`, `:154-190`, `:258-276`). Its context builder keeps the latest compaction entry and retained tail while dropping older summarized entries (`pi/packages/agent/src/harness/session/context.ts:45-63`, `:65-100`).

Pi's compaction is also a clean split. It estimates tokens from provider usage plus a fallback heuristic, reserves a budget, and selects a valid cut point (`pi/packages/agent/src/harness/compaction/compaction.ts:147-162`, `:183-249`, `:373-421`). It prepares a retained tail and summary source, calls a no-tools summary completion, then persists a compaction entry (`pi/packages/agent/src/harness/compaction/compaction.ts:615-687`, `:706-760`; `pi/packages/coding-agent/src/core/agent-session.ts:1929-1955`).

Copy these session rules into Wk: append before advancing the snapshot, replay on restart, keep an explicit compaction marker, and derive context from the active branch rather than from provider history. Extend the proposed `conversation.jsonl` rows with `parent_id` and a `main` lane leaf. Wk can keep one lane in the first release, but this matches `WkSessionTree` and leaves room for replace or branch operations (`backend/app/agent_runtime/wk_common.py:34-70`). Keep the existing `run.json`, `raw.jsonl`, `events.jsonl`, supervisor projection, and archive ownership. Pi's session header and schema are not compatible with Wk's run files.

### Conflicts and recommendations

| Topic | Pi approach | Wk constraint or current choice | Recommendation |
|---|---|---|---|
| Provider channel | Direct Anthropic and ChatGPT HTTP/WebSocket APIs with OAuth-derived request tokens. | The ticket forbids raw provider APIs and asks for plan-authenticated Claude/Codex backends. | Keep CLI `stream-json` for Claude and App Server stdio for Codex, subject to the single-completion boundary tests. If either fails, make the failure a Henry decision; do not silently ship a provider-owned loop. |
| Provider caching and thinking | Pi keeps prompt-cache identity, thinking blocks, usage, and encrypted Codex content in the common stream. | Rehydrating Wk context on every turn costs cache reuse. Codex private reasoning remains unavailable in current Wk events. | Accept the cache trade-off for deterministic Wk state. Preserve public thinking summaries and usage. Revisit a plan-authenticated provider cache only after session parity. |
| Context model | Parent-linked tree plus explicit compaction entries and retained tails. | The first Wk draft described a linear conversation log, while Wk already has a parent-linked event tree. | Add `parent_id` and `main` leaf metadata now. Keep a one-lane projection first; do not import pi's session format. |
| Tools | One `AgentTool[]` registry. Provider adapters serialize it to each API. | Wk also needs mutation classes, gate receipts, native-tool rejection, and `WkToolLedger`. | Copy the one-registry rule. Keep Wk's ledger and role policy as the stronger contract. |
| Approvals | Hook can block a tool and return an error result. No durable approval event. | Supervisor and TUI require persistent pending requests and provider approval responses. | Build `WkApprovalPolicy` as planned. Use pi's pre-execution hook shape inside the policy, not as the persistence model. |
| Events | Small generic agent lifecycle with nested provider deltas. | `normalizer.py`, raw-first durability, `WkEventEnvelope`, and lane lifecycle are compatibility contracts. | Copy generic lifecycle boundaries. Keep existing normalized kinds and raw replay. |
| Persistence ordering | Pi's coding host emits listeners before appending regular messages (`pi/packages/coding-agent/src/core/agent-session.ts:644-668`). | Wk must preserve raw-first durability and pending-request durability before supervisor publication. | Persist conversation and raw provider data before publishing user-visible completion or approval events. |

The pi-style direct API design is the only inspected reference that clearly gives the harness a true completion boundary. It also violates the explicit raw-API constraint. The selected Wk transport seams are therefore conditional, not proven. WIKI-363 and WIKI-364 must test: model delta, tool call before execution, Wk result submission, next completion, approval pause, interruption, and restart with the provider process gone. A transport that only lets Wk execute an MCP or dynamic tool while the provider owns continuation does not satisfy full loop ownership.

### Impact on the WIKI-362+ ladder

- WIKI-362 must define a `stream_function`-like completion contract: provider input is assembled by Wk, provider output is a stream, tool calls stop at a Wk boundary, and no provider transcript is required for the next request.
- WIKI-363 and WIKI-364 must include the boundary test as a hard gate. A passing tool bridge without Wk-controlled continuation is not parity.
- WIKI-365 should store `parent_id`, `context_generation`, provider-ID mappings, and the `main` leaf beside each canonical message. The existing linear projection can remain the first reader.
- WIKI-366 should follow pi's pure preparation and summary steps, then add Wk-specific retention for gate receipts, pending approvals, and open ledger mutations.
- WIKI-368 should expose a persistent approval request even when a provider has no native approval wire. The provider adapter only translates a final response.
- WIKI-369 should map pi-like lifecycle events into `normalizer.py` kinds and preserve raw provider frames for replay.

## Target architecture

### Modules

Keep the current modules where they already have a clear contract. Add small modules for the missing ownership boundaries.

| Module | Responsibility |
|---|---|
| `wk_core.py` | Provider-neutral event envelope, typed tool request/result, loop-owned status, integrity, and lifecycle contracts. Extend the contracts; do not put provider imports here. |
| `wk_session.py` | `WkConversationStore`, append-only typed messages, snapshots, replay, compaction markers, and session schema migration. It writes beside `run.json`, `raw.jsonl`, and `events.jsonl`. |
| `wk_context.py` | `WkContextAssembler`, token estimate, prompt budget, retained-message rules, and compaction policy. It returns an immutable `CompletionContext` with a digest. |
| `wk_approval.py` | `WkApprovalPolicy`, `ApprovalRequest`, `ApprovalDecision`, pending-request persistence, stale-ID checks, and provider response translation. |
| `wk_loop.py` | The single turn loop. It owns user messages, completion calls, streamed assistant content, tool calls, tool results, approvals, compaction, interruption, steering, and final turn state. |
| `wk_backend.py` | `WkCompletionBackend` protocol. It exposes plan-auth verification, one completion stream, tool-result submission, approval response, interrupt, and close. It has no message store. |
| `wk_claude.py` | Thin Claude CLI stream-json backend. Keep auth and wire translation. Remove provider-side loop decisions from this layer. Keep the SDK lane behind the old wk flag during migration. |
| `wk_codex.py` | Thin Codex App Server backend. Keep account proof, JSON-RPC, dynamic-tool wire translation, and provider process control. Do not make App Server history authoritative. |
| `wk_tools.py` | One registry and one implementation set for both providers. Keep `WkToolRequest`, `WkToolResult`, mutation classes, receipts, and role-specific gate behavior. |
| `wk_adapter.py` | Supervisor adapter that attaches one `WkLoop` to one run, routes raw and normalized events, projects status, and exposes supervisor controls. It does not assemble prompts. |
| `normalizer.py` | Existing provider-neutral event taxonomy. Add only the smallest new mappings needed for canonical wk tool, approval, and compaction events. Keep existing raw-provider mappings stable. |
| `wk_tui/engine.py` | Existing lane lifecycle and process-reap machinery. It remains independent of Rich and prompt-toolkit (`backend/app/agent_runtime/wk_tui/engine.py:30-50`, `:172-223`, `:317-346`). |
| `wk_tui/render.py` | Existing raw event renderer. It continues to consume Claude and Codex raw provider shapes (`backend/app/agent_runtime/wk_tui/render.py:247-398`). Add canonical approval and partial-text render cases only when the event contract proves necessary. |

The current `wk_claude.py` duplicate tool classes should not survive the migration. `register_default_wk_tools` already imports the shared implementations for both lanes (`backend/app/agent_runtime/wk_tools.py:514-549`, `backend/app/agent_runtime/wk_claude.py:965-969`). WIKI-367 removes or quarantines the duplicate definitions after the old lane tests pass.

`wk.archive` needs an explicit decision. It appears in the allowed name set (`backend/app/agent_runtime/wk_core.py:382-384`) but is not registered by `register_default_wk_tools` (`backend/app/agent_runtime/wk_tools.py:839-849`) and is not in the default bridge allowlist (`backend/app/agent_runtime/wk_common.py:505-519`). Keep archive as a supervisor control, not a model tool, and remove the name from the model registry contract. This avoids giving a model a terminal run operation.

### Data flow

1. The supervisor creates a `RunRecord` and attaches a `WkProviderAdapter`, as it does today (`backend/app/agent_runtime/supervisor.py:2618-2773`). The adapter chooses the new loop only when the new wk flag is enabled. Legacy `cc`, `cdx`, and current wk lanes remain selectable.
2. `WkLoop` opens `WkConversationStore`, replays the durable conversation, restores pending approvals and the ledger, and checks the worktree and plan-auth identity.
3. A user prompt or supervisor steer is appended as a canonical user message before the provider call. The loop asks `WkContextAssembler` for a bounded `CompletionContext` and records its digest.
4. `WkCompletionBackend` starts one provider completion. Raw provider frames are emitted in their existing Claude or Codex shape. The backend attaches generation and direction metadata but does not rewrite the provider payload.
5. The loop passes each raw frame through `normalizer.normalize_provider_event`. It emits the normalizer kind, disposition, and lifecycle state. The supervisor continues its existing raw-first and normalized-second writes (`backend/app/agent_runtime/supervisor.py:1241-1341`).
6. Assistant text and public reasoning summaries append to the conversation store. Partial text is streamed as events but commits to a message only at the provider turn boundary or the TUI commit-on-newline boundary.
7. A Claude MCP call or Codex `item/tool/call` becomes one `WkToolRequest`. `WkApprovalPolicy` evaluates it. If allowed, `WkToolRegistry` validates and executes it. `WkToolLedger` records `tool.started` before execution and `tool.completed` or `tool.failed` after execution. The typed result returns through the backend wire.
8. A provider approval or user-input request becomes one canonical `approval` event. The loop persists it in the conversation store and supervisor projection before publishing it. The TUI or API calls `respond`; the policy validates the answer and the backend carries it to the provider.
9. When the provider produces a final result, the loop appends the assistant message and turn boundary, updates status, and drains queued idle work. It does not trust a provider transcript to recover the message list.
10. The supervisor receives the same raw and normalized event shapes as today. It updates RunRecord, SQLite projections, pending requests, unread state, and SSE invalidation. The TUI consumes the event stream without a provider-specific second loop.

### Canonical message format

Use a versioned JSONL file at `<run-dir>/conversation.jsonl`. Keep provider frames in `raw.jsonl`; do not merge the two formats.

Each row has a monotonically increasing conversation sequence, a stable message ID, a `parent_id`, and a lane. Roles are `system`, `user`, `assistant`, `tool_call`, `tool_result`, `approval`, and `compaction`. Store public text, structured tool arguments, typed tool results, provider-neutral IDs, turn ID, and context generation. Do not store API keys or private Codex reasoning.

```json
{"schema":"wiki.wk.conversation.v1","seq":12,"message_id":"m-12","parent_id":"m-11","lane":"main","turn_id":"t-4","role":"tool_result","tool_call_id":"call-7","tool_name":"wk.read","content":[{"type":"text","text":"..."}],"result":{"success":true,"exit_code":0},"context_generation":1,"created_at":"2026-08-19T00:00:00+00:00"}
```

The run snapshot needs only small scalars: conversation sequence, context generation, context digest, last completed turn, provider session handle, and session schema version. `RunRecord` already persists provider session identity and event projections (`backend/app/agent_runtime/types.py:327-409`). Add the wk conversation fields without removing existing fields.

Archive `conversation.jsonl`, `context.json`, and the schema metadata with the existing archive copy set (`backend/app/agent_runtime/store.py:1389-1457`). A crash after the conversation append but before `run.json` update must be repaired by replay, as the existing raw and normalized counters are repaired today (`backend/app/agent_runtime/store.py:3348-3489`).

### Context and compaction policy

`WkContextAssembler` receives the canonical messages, system instructions, current worktree identity, tool schemas, role, provider, model, and maximum input budget.

The default policy is:

- Preserve the system contract, current user request, all open tool calls, all pending approvals, the latest assistant answer, and the current status/PR facts.
- Keep complete recent turns until the estimated input reaches 80% of the configured budget.
- Compact only completed turns. Never compact between `tool_call` and `tool_result`.
- Summarize older completed turns through a no-tools backend completion, then persist one `compaction` message with source sequence range, summary digest, and context generation.
- If summarization fails, keep the full context and block before sending an over-budget request. Do not silently drop messages.
- Include the last successful gate receipt and later mutation state in the retained facts. A compaction must not make `WkLoop.validate_merge_ready()` lose evidence.
- Treat provider `compact_boundary` and `context/compacted` events as observed provider behavior. They do not alter the wk store.

The compaction summary is public model output. Do not copy encrypted Codex reasoning. Keep the provider-reported token usage as telemetry. Use the same deterministic context digest on resume to detect a changed policy or corrupted session.

### Tool dispatch and approvals

The registry stays provider-neutral. The provider adapters translate only these wires:

- Claude `mcp__wiki__<name>` -> `wk.<name>`.
- Codex namespace `wiki`, tool `<name>` -> `wk.<name>`.

The loop creates the request ID if the provider does not supply one. The provider ID remains in the raw payload and a mapping table. The ledger records the wk ID, provider ID, input hash, mutation class, result hash, and receipt.

`WkApprovalPolicy` has three outcomes:

- `allow`: execute the registered Wiki tool.
- `deny`: return a typed denied result and keep the run alive when the provider supports a tool result.
- `ask`: persist a pending approval and pause the loop in `WAITING_APPROVAL`.

The policy is role- and mode-aware. Unattended review workers keep their current safe default for Wiki tools. Native provider tools, unknown namespaces, malformed arguments, and stale generation IDs always deny or block. The TUI can select `ask` for configured mutation tools. The backend cannot bypass the policy by setting `approvalPolicy=never`; that provider setting only prevents native Codex execution.

Persist approval rows before publishing them. On restart, restore pending rows and require the provider to re-emit a current request or use the new loop's canonical pending request. Never replay a stale provider response by numeric ID without the generation and provider mapping check. Existing supervisor type-safe request keys are the model (`backend/app/agent_runtime/store.py:208-234`, `:278-308`).

### Event surface

Provider content events must use the current `normalizer.py` taxonomy. Do not make `render.py` learn a second wk-specific provider taxonomy.

For provider frames, the event object contains:

- `raw`: the unchanged Claude or Codex provider payload.
- `normalized`: `kind`, `disposition`, `lifecycle_state`, and bounded provider-neutral payload from `normalize_provider_event`.
- `wk`: run, turn, generation, conversation sequence, and context digest metadata.

Keep `WkEventEnvelope` for wk-owned ledger, status, compaction, approval, and archive events. Those events already have phases and dispositions (`backend/app/agent_runtime/wk_core.py:33-40`, `:140-186`). Provider content should not be renamed from `claude_assistant` or `item_completed` to an unrelated `wk.*` kind.

This avoids a current ambiguity: the wk translators create `claude.<type>` and `codex.<normalized kind>` envelopes (`backend/app/agent_runtime/wk_claude.py:485-502`, `backend/app/agent_runtime/wk_codex.py:322-361`), while `normalizer.py` has its own `claude_<type>` and method-derived taxonomy (`backend/app/agent_runtime/normalizer.py:164-209`, `:267-389`). WIKI-369 makes one taxonomy authoritative for stream consumers while preserving v0 envelope replay for old runs.

The supervisor already writes raw first, then normalizes, materializes, updates wk status, and publishes the session invalidation (`backend/app/agent_runtime/supervisor.py:1241-1466`). The new loop should feed that path, not bypass it.

## Migration ladder

All new stages are additive. `WIKI_ENABLE_WK=1` remains the outer gate. Add a second loop selector with `legacy-provider-loop` and `wk-loop-v1` values. The default remains the current path until the stage's parity gate passes.

| Ticket | One lane-sized contract | Depends | Regression anchor |
|---|---|---|---|
| WIKI-362 | Define `WkMessage`, `WkConversationStore` interfaces, `CompletionContext`, backend events, provider ID mapping, and versioned loop states. Add fixtures for one Claude and one Codex turn. No provider behavior changes. | WIKI-345, WIKI-346 | `backend/tests/test_wk_core.py:224-324`; `backend/tests/test_wk_render.py:386-467`; existing normalizer disposition tests in `backend/tests/test_agent_runtime_store.py:151-295`. |
| WIKI-363 | Implement and probe the Claude CLI stream-json backend. Prove plan auth, no API credential environment, no native tools, a tool call delivered before execution, Wk-controlled result submission and next completion, partial stream order, interruption, and clean exit. Keep `WkClaudeLane` SDK path as the fallback. | WIKI-362 | `backend/tests/test_wk_claude.py:102-249`, `:318-455`; `backend/tests/test_agent_runtime_adapters.py:723-829`; `backend/tests/fixtures/agent_runtime/claude_sdk_lane_events.jsonl`. |
| WIKI-364 | Implement the Codex App Server backend contract. Keep exact-connection `account/read`, dynamic Wiki tools, native-tool rejection, approval wire, turn interrupt, and thread handle reporting. Prove the App Server returns control before a tool executes, or fail the wk-loop seam. Do not yet change supervisor routing. | WIKI-362 | `backend/tests/test_wk_codex.py:167-267`, `:318-449`; `backend/tests/fixtures/agent_runtime/wk_codex_app_server.py`. |
| WIKI-365 | Persist canonical conversation rows, compaction markers, session schema, and replay. Add archive copy and crash-replay repair. Existing raw/event files remain unchanged. | WIKI-362 | `backend/tests/test_agent_runtime_store.py:640-726`, `:1807-1833`, `:2559-2660`; `backend/tests/test_wk_supervisor.py:345-409`. |
| WIKI-366 | Add deterministic context assembly, input budgeting, summary compaction, context digest, and compaction telemetry. Provider compaction events cannot mutate the canonical store. | WIKI-363, WIKI-364, WIKI-365 | New unit fixtures for open tool calls, pending approvals, failed compaction, and summary replay; existing Codex `context/compacted` classification at `backend/app/agent_runtime/normalizer.py:18-39`. |
| WIKI-367 | Route both provider tool wires through the one `wk_tools.py` registry and `WkToolLedger`. Delete the duplicate Claude tool classes after parity. Keep archive as supervisor control, not a model tool. | WIKI-363, WIKI-364 | `backend/tests/test_wk_claude.py:472-764`; `backend/tests/test_wk_codex.py:231-449`; `backend/tests/test_wk_core.py:324`; `backend/tests/test_wk_integrity.py`. |
| WIKI-368 | Add the approval policy and durable pending request state. Implement allow, deny, ask, stale response, duplicate response, restart re-emit, and TUI answer contracts. Provider adapters only translate the final response. | WIKI-365, WIKI-367 | `backend/tests/test_agent_runtime_adapters.py:466-575`, `:882-1254`; `backend/tests/test_agent_runtime_store.py:890-1016`; `backend/tests/test_agent_runtime_supervisor.py:2612-2649`. |
| WIKI-369 | Make the loop stream raw provider frames plus normalizer taxonomy. Add newline commit buffering and preserve the supervisor's raw-first dual write. Do not add TUI dependencies. | WIKI-363, WIKI-364, WIKI-367 | `backend/tests/test_wk_render.py:42-398`, `:540-664`; `backend/tests/test_wk_cli.py:228-253`; `backend/tests/test_event_store_supervisor.py:97-174`. |
| WIKI-370 | Attach the new `WkLoop` to the supervisor. Route start, send-now, send-on-idle, interrupt, replace, archive, and exact restart recovery. Run old and new loops under one feature selector. | WIKI-365, WIKI-366, WIKI-368, WIKI-369 | `backend/tests/test_wk_supervisor.py:214-409`; `backend/tests/test_agent_runtime_store.py:514-588`, `:1917-2060`; full supervisor adapter fixtures. |
| WIKI-371 | Enable the new loop for a bounded canary. Compare canonical messages, tool ledger, approval projections, lifecycle state, normalized events, archive contents, and resume behavior against the old loop. Add rollback metrics and a kill switch. | WIKI-370 | `backend/tests/test_wk_supervisor.py`; `backend/tests/test_agent_runtime_store.py:2559-3232`; captured live-lane parity corpus. |

### TUI ladder changes

WIKI-345 and WIKI-346 are already the correct foundation. Keep their engine and renderer extraction. The parked WIKI-347 shell does not need a new architecture. Its composer, `patch_stdout` pump, status line, history, and interrupt ladder should call the new `WkLoop` control surface and consume the WIKI-369 stream.

Retarget the existing TUI tickets as follows:

| Ticket | Revised dependency and contract | What survives from the parked design |
|---|---|---|
| WIKI-347 | Depends on WIKI-362 and WIKI-369. Build the inline renderer, sticky composer, status bar, history, and interrupt shell against `WkRunControl` and canonical events. | The non-alt-screen layout, `patch_stdout`, background asyncio loop, status shell, three-strike interrupt ladder, and provider process reaping from `wk_tui/engine.py:172-223`, `:317-346` survive. The driver selection in `wk_cli.py:247-270` stays unchanged. |
| WIKI-348 | Depends on WIKI-347 and WIKI-369. Add commit-on-newline token streaming. Buffer normalized text deltas, flush complete lines to scrollback, and keep the partial line in the status area. | The locked streaming behavior in `vault/wiki-app/wk-tui-design.md:30-34` survives. The input source changes from raw provider polling to the WIKI-369 stream. |
| WIKI-349 | Depends on WIKI-347 and WIKI-365. Implement `/model`, `/effort`, `/clear`, `/verbose`, `/help`, and `/quit` as loop commands. `/clear` resets the canonical conversation through an explicit store operation. | The command list and plain fallback remain. No command may edit provider transcript files directly. |
| WIKI-350 | Depends on WIKI-347 and WIKI-368. Render canonical approval requests as inline cards and answer through `WkLoop.respond`. | The inline y/n(/always) composer mode from `vault/wiki-app/wk-tui-design.md:34-38` survives. Approval IDs and policy decisions move to wk. |
| WIKI-351 | Depends on WIKI-347, WIKI-365, and WIKI-370. Implement `--continue` and `--resume` by loading the wk session store, then reattaching or recreating the provider transport. | The CLI flags and provider session capture remain. The canonical transcript becomes wk `conversation.jsonl`, not the native provider file. |

The plain path remains a hard regression boundary. `wk_cli.py` still selects plain mode for `-p`, `--plain`, non-TTY output, or TUI import failure (`backend/app/agent_runtime/wk_cli.py:247-270`). `backend/tests/test_wk_cli.py` must pass unchanged through WIKI-347 and every later TUI ticket.

### Per-stage rollback rule

Every stage adds a flag or an adapter boundary. A failed seam test leaves the previous lane enabled. A failed live parity canary disables `wk-loop-v1` without deleting the canonical session files or provider transcripts. Do not remove `claude_agent_sdk`, `ClaudeStreamAdapter`, or `CodexAppServerAdapter` until WIKI-371 has passed two restart and approval cycles for each provider.

## Risks and decisions for Henry

### Decisions to lock before implementation

1. **Claude CLI contract.** Approve the CLI stream-json plus MCP bridge as the primary seam, subject to the WIKI-363 proof. If the installed CLI cannot disable native tools while exposing the wk bridge, keep the SDK lane and treat the design as blocked rather than allowing a mixed tool owner.
2. **Provider cache trade-off.** The target treats provider sessions as disposable completion transports. This costs prompt tokens and latency but makes wk context and resume authoritative. Decide whether this cost is acceptable for review workers and the TUI.
3. **Compaction summarizer.** The first implementation should use the same plan-authenticated backend with tools disabled. Decide whether a local deterministic fallback is required. The safe default is to block on failed summarization rather than drop context.
4. **Interactive policy defaults.** Decide which mutation tools ask in TUI mode. The unattended review path must keep explicit role policy and must not inherit a permissive TUI default.
5. **Session compatibility.** New wk sessions are not byte-compatible with Claude native JSONL or Codex App Server history. Resume old runs through the old adapters. Resume new runs from `conversation.jsonl`. Do not attempt an implicit provider transcript conversion.
6. **Schema versioning.** Keep `wiki.wk.event.v0` replayable. Add the conversation schema separately. If the provider stream envelope changes, support v0 read and v1 write during one release.
7. **Completion boundary.** A tool bridge is not enough. WIKI-363 and WIKI-364 must show that wk receives a tool call before execution and starts the next provider completion after the result. If neither process seam passes, Henry must choose between relaxing the raw-API ban and accepting provider-inner-loop behavior.

### Main risks and controls

| Risk | Why it matters | Control |
|---|---|---|
| Provider behavior drift | CLI flags, MCP startup records, App Server method shapes, and auth fields can change. | Startup attestation, fixture tests, binary-version capture, fail-closed policy, and per-provider canaries. |
| Missing completion boundary | CLI MCP or App Server dynamic tools may let wk execute a tool while the provider still owns continuation. | Make pre-execution tool-call delivery a hard WIKI-363/WIKI-364 gate. Keep the old path enabled until a seam passes. Escalate the raw-API trade-off if both fail. |
| Compaction quality | SDK and App Server compaction may preserve facts better than a first wk summary. | Golden long-context cases, retained-fact assertions, context digest, summary source ranges, and a block-on-failure policy. |
| Tool duplication | A provider-native tool or a second bridge can mutate the worktree outside the ledger. | Exact tool-set attestation, native-tool rejection, one registry, protected status path checks, and ledger reconciliation. |
| Approval races | Provider request IDs can be reused, resolved late, or re-emitted after restart. | Provider-plus-generation mapping, durable pending rows before publication, type-safe IDs, idempotent responses, and restart fixtures. |
| Event contract drift | Changing wk envelope kinds can duplicate or hide transcript rows. | Keep raw payloads unchanged, use `normalizer.py` taxonomy, dual-write old and new representations, and compare SQLite projections. |
| Session loss | Native transcripts may be absent, rewritten, or unavailable after a provider upgrade. | Canonical wk conversation log, fsynced append before provider delivery, replay repair, and archive copy before hot-store removal. |
| Resume semantics | Replaying a user message can duplicate a tool mutation. | Persist tool start and result, reconcile open mutations, resume only at an explicit turn boundary, and block if the last operation is incomplete. |
| Supervisor coupling | Moving fleet locks or archive decisions into wk could break recovery. | Keep RunStore, supervisor locks, PID identity, raw-first ingest, and archive ownership in the supervisor. |
| Thinking data limits | Codex private reasoning is not available. | Store only provider summaries and preserve the existing encrypted-capability boundary. |
| Irreversible cleanup | Removing old adapters or native session files would make rollback impossible. | No deletion before WIKI-371. Archive both formats during the migration window. |

### Acceptance gate for the architecture

The design is ready to implement when each provider can complete the same lane contract through the new loop:

- start with plan auth and no API credential source;
- stream assistant text and provider lifecycle events;
- receive a tool call before provider execution, run it in Wk, submit the result, and start the next completion;
- call one read tool and one mutation tool through the single registry;
- pause for an approval, answer it, and resume;
- compact a long completed history without losing the current task, gate receipt, or open operation;
- interrupt and restart without duplicating the last mutation;
- archive `run.json`, raw events, normalized events, conversation, and compaction state;
- resume from wk state after the provider process is gone;
- render through the existing normalizer and `wk_tui/render.py` paths;
- leave the legacy path and plain `./wk` path unchanged.

This gives Henry a reversible path from the current Agent SDK and App Server wrappers to a wk-owned loop without making provider behavior or native provider session files the next architectural dependency.
