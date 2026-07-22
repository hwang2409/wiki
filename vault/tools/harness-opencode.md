---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Harness: OpenCode internals

Source: `misc/open-docs/docs/opencode/` (single-pass mining 2026-07-21, workflow `open-docs-harness-mining`). Not adversarially verified — treat as "docs claim X", not first-hand code audit.

## Summary

OpenCode is a 100% open-source, provider-agnostic AI coding agent built for terminal-first workflows. It separates concerns via client/server architecture (server on machine, multiple clients: TUI, desktop, IDE via ACP). Uses Bun+TypeScript throughout, Hono for HTTP, integrates Vercel AI SDK for multi-provider support (Anthropic/OpenAI/Google/Bedrock). Unique design bets: descending session IDs, Git-based snapshots, session compaction with two-phase pruning, configurable AGENTS.md discovery, Zod-validated prompt assembly, and namespace-based module organization with event-driven patterns.

## Architecture

**Client/Server Split**: Monorepo with server (`packages/opencode/src/server/` — Hono HTTP 44KB) handling AI+files+state, multiple clients (TUI in Go soon TypeScript, desktop SolidJS, web Astro, IDE via ACP JSON-RPC over stdio). Lazy-loaded Hono app. Communication: HTTP/REST, SSE streaming, WebSocket, JSON-RPC (ACP).

**Process Model**: Single server per machine (configurable port), multiple TUI/desktop clients connect. Server holds all session state, LSP servers (per-language, auto-downloaded to `~/.opencode/lsp/`), file watchers (Parcel/chokidar).

**Event Loop**: Bun-native event loop, no process-based concurrency. Session locks use AbortController + Symbol.dispose for cleanup. Scheduler.register() for background tasks (snapshots, LSP health 5m, pruning 24h).

**State Model**: File-based storage at `~/.opencode/data/projects/{projectID}/sessions/{sessionID}/` — one JSON per session info, one JSON per message, one folder per message containing parts. Snapshot Git repos at `~/.opencode/data/snapshot/{projectID}/` for undo. Storage layer abstraction in `storage/index.ts`.

**Streaming**: Vercel AI SDK `streamText()` for LLM, SSE endpoints for TUI, tool results streamed back to model immediately. Message parts have delta-streaming support (text accumulation during streaming).

## Tools & execution

**15+ built-in tools** (registry in `tool/registry.ts`): read, write, edit, multiedit, bash, grep, glob, ls, lsp-diagnostics, lsp-hover, patch, task, todo, webfetch (+ MCP tools injected). All validated with Zod schemas before execution. Tool context includes: sessionID, messageID, agent name, AbortSignal (cancellation), callID.

**Tool Execution Flow**: Tool call parsed from AI → validate schema → check permission → create tool part (status=active) → execute with timeout → update part (status=completed or error) → stream result back to AI. Permissions: "allow"/"deny"/"ask" per tool per agent, configurable in config.json or .opencode/agent/*.md.

**Custom Tools**: Discovered from `.opencode/tool/*.ts` and `~/.opencode/tool/*.ts`, loaded dynamically via Bun.Glob. Plugin tools loaded from plugins via Plugin.list().

**Parallel Execution**: AI SDK handles tool-call streaming; multiple tool calls in same response can execute in series (not parallel in current impl, but future-proofed). Tool outputs fed back to model via tool-result in message stream.

**Tool Permissions**: Bash has per-command granularity (e.g., `"npm test": "allow"`, `"rm -rf": "deny"`). Dangerous patterns detected regex (rm -rf /, mkfs, chmod 777 /). File edits always validated to project root, no escape via symlinks. Web tools blocked to private IPs. 30s default timeout, 10m hard limit.

## Context & prompts

**System Prompt Stack** (highest priority first):
1. Provider header (spoofing e.g., "You are Claude...") — optional per provider
2. Provider-specific prompt (Anthropic/OpenAI/Gemini/etc differ)
3. Environment info (working dir, git status, platform, date, tree snippet via ripgrep)
4. Custom instructions from AGENTS.md files (discovered: local AGENTS.md > packages/*/AGENTS.md > ~/.opencode/AGENTS.md > ~/.claude/CLAUDE.md > config.instructions glob patterns)
5. Agent-specific prompt (from .opencode/agent/{agentName}.md)
6. User override (--system flag, highest override priority)

**Prompt Assembly** (`session/prompt.ts` 54KB, most complex module): context prioritized as system > recent messages > active files > project context > historical summaries > LSP diagnostics. Truncation order: background (LSP) first, then historical, then active files, never recent messages. File inclusion: recent access time, explicit refs in messages, LSP imports/exports, relevance scoring. Prompt compaction checks isOverflow() before streaming: if tokens > model.context * 0.8, runs compaction.

**AGENTS.md Format**: Markdown with sections: H1 name, description, ## Guidelines (bullets), ## Allowed (actions), ## Forbidden (actions), ## Context, ## Commands (custom). Inheritance: files found first stop search for that name (e.g., project/AGENTS.md overrides ~/.opencode/AGENTS.md). Supports tilde expansion, glob patterns in config instructions.

**Prompt Caching**: Not explicit in docs; uses Vercel AI SDK which may cache via provider (e.g., Anthropic's prompt caching if enabled). Token tracking: input/output/cache.read, cost calc per model's costPer1kInput/Output. Max output tokens: 32,000 constant.

## Session & state

**Session Schema** (Zod in `session/index.ts`): id (descending ID format "sess_*"), projectID, directory, parentID (optional, for forks), summary (FileDiff array), share (url), title, version, time (created, updated, compacting?), revert (messageID, partID, snapshot?, diff?).

**Message Schema** (MessageV2.Info): id (ascending "msg_*"), sessionID, parentID?, role (user|assistant), system[], mode (build|chat), path (cwd, root), modelID?, providerID?, cost?, tokens?, error?, summary? (bool).

**Message Parts** (MessageV2.Part types): text, reasoning, tool, retry, step-start, step-finish. Tool parts: tool name, input/output, state (status: active|completed|error), time (start, completed, compacted?).

**Lock Mechanism** (`session/lock.ts`): Advisory locks using Map<sessionID, {controller: AbortController, created: number}>. Acquired via SessionLock.acquire() returns disposable with [Symbol.dispose] for auto-cleanup. Throws LockedError if already locked. Force abort on instance disposal.

**Compaction** (`session/compaction.ts`): Two phases. Phase 1 (Prune): iterate messages backward, skip last 2 turns, mark tool outputs compacted, save 20-40K tokens. Phase 2 (Summarize): send non-compacted messages to AI with summarization prompt, create summary message (summary: true), retry up to 10x with exponential backoff.

**Snapshot System**: Git-based, created before write/edit/multiedit/patch tool execution via Snapshot.track(filePath) → returns hash. Restore via Snapshot.restore(hash). Cleanup runs daily via Scheduler (7-day retention default).

**Fork/Branch**: Session.fork(sessionID, messageID?) creates child session with parentID, copies all messages up to messageID, generates new IDs, independent from parent after creation.

**Resume**: `--continue` flag continues last session; `--session <id>` continues specific session. Messages loaded in ascending ID order (chronological).

## Safety & permissions

**Permission Levels**: "allow" (execute immediately), "deny" (reject auto), "ask" (prompt user approval).

**Permission Types**: edit (file operations), bash (shell commands), webfetch (web access). Bash has record granularity: `{"npm test": "allow", "*": "ask"}`.

**Permission Modes**: default (ask for each), auto-edit (approve file ops), auto-approve (approve most except dangerous), yolo (approve all). Set via --permission flag or OPENCODE_PERMISSION env var. Default mode configs in .opencode/config.json agents.{name}.permission.

**Dangerous Patterns Detected** (regex): rm -rf /, mkfs, dd if=, chmod 777 /, curl|bash, wget|bash, sudo, chmod -R 777 /, :(){:|:&};: (fork bomb).

**Sandbox Mechanisms**: 
- Working directory validation (all paths checked against project root via Filesystem.contains())
- Symlink resolution to prevent escapes
- Absolute path→relative conversion
- Bash timeout: default 30s (env var OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS), hard limit 10m
- Output truncation: default 100KB (env var OPENCODE_EXPERIMENTAL_BASH_MAX_OUTPUT_LENGTH)
- Web tool limits: 10MB response, 30s timeout, blocks executables/archives, private IPs blocked
- File path traversal patterns blocked: ../../, /etc/, ~/.ssh/

**Permission Workflow**: Tool called → check agent.permission[type] → if "allow" execute, if "deny" error, if "ask" show prompt (user can approve once or always). Always approval caches in session config.

**No inherent network sandbox**, but web tool URL validation blocks localhost/private IPs (unless configured). MCP servers run as child processes, inherit parent env (potential vector).

**Audit Trail**: All tool executions recorded as tool parts in session messages. No separate audit log file mentioned, but messages are persisted to disk and can be reviewed.

## Extensibility

**Plugin System** (`plugin/`): Plugins loaded via Plugin.list(), export tools via plugin.tool. Discovered from config or auto-loaded from directories (details sparse in docs).

**MCP Integration** (`mcp/index.ts`): Servers configured in .opencode/config.json under mcp.servers as {name: {command, args, env}}. Tools/resources from MCP injected into ToolRegistry. CLI: `opencode mcp add <package>`, `opencode mcp list`, `opencode mcp test <server>`. Supports env var interpolation (${VAR}).

**Custom Tools**: Place in .opencode/tool/*.ts. Export default Tool.define() or Tool.Info. Loaded dynamically at registry init time. Full access to Instance context (directory, worktree, etc.).

**Skills System** (NEW in v1.0+): Discovered from `.opencode/skill/*.md` and `.claude/skills/*.md`. Files are SKILL.md with frontmatter metadata. Project-level + global support. Compatible with Claude Code skill format.

**Config System**: Supports .opencode/config.json (JSONC with comments via jsonc-parser) or config.jsonc. Deep merge: global > project ancestors > CLI override. Env vars: OPENCODE_CONFIG_CONTENT (inline JSON), OPENCODE_CONFIG_DIR (custom dir).

**Provider Extension**: Custom providers can be added by implementing Provider.Info interface with models() and language (Vercel AI SDK model) fields. Example stub in docs but no registry extension point shown.

**LSP Extension**: Configure per-language server commands in config.lsp. Can point to custom LSP binaries. Auto-download disabled via OPENCODE_DISABLE_LSP_DOWNLOAD.

**AGENTS.md Inheritance**: Files inherit/cascade; most specific found first wins for that filename type. No explicit merge/concat of multiple files (first found stops search).

**Hooks**: No explicit hook system documented (unlike Claude Code). Settings.json analog is config.json. No before/after triggers for commands.

## Notable / surprising

**Descending Session IDs**: Sessions use `Identifier.descending("session")` — newer sessions have *smaller* IDs. Unusual but enables efficient "latest session" queries.

**Ascending Message IDs**: Messages use `Identifier.ascending("message")` — ensures chronological ordering without sorting.

**Prompt "Spoofing"**: For non-Anthropic providers, system prompts include "You are Claude..." identity assertion. Improves model behavior but technically spoofs identity.

**Bun-first**: Uses Bun native APIs (Bun.file(), Bun.write(), Bun.Glob, Bun.spawn()) over Node equivalents. Not polyfilled for Node.js — OpenCode is Bun-only at runtime.

**No Explicit Prompt Caching**: Vercel AI SDK may use provider caching (Anthropic's native feature), but no explicit cache management layer in docs. Token counts tracked but no reuse metrics.

**Namespace + Event Pattern Over Classes**: Core modules (Session, Storage, Tool, etc.) implemented as TypeScript namespaces with static methods + event bus, not OOP classes. Enables tree-shaking.

**Symbol.dispose For Cleanup**: Uses TC39 Symbol.dispose (Stage 3) with `await using` syntax. Future-proofs resource cleanup but requires TS/Node 22.6+.

**Git-Based Snapshots**: File history managed via embedded Git repos (not just diffs). snapshot/restore go through Bun.spawn("git"). Unique compared to diff-based undo.

**No Web Console Auth Mentioned In Core Docs**: Web console (packages/console/) references Drizzle ORM + serverless functions but core opencode docs don't detail auth beyond "identity management". May be cloud-hosted.

**Tool Part Delta Streaming**: Tool results feed back to model via streaming parts with delta support, enabling real-time tool results in reasoning loops.

**Environment Auto-Injection**: Every prompt includes live env info (working dir, git status, platform, date) — not static, evaluated at prompt time.

**Compaction Retry Backoff**: Exponential backoff for compaction (up to 10 retries, max 60s per attempt). Compaction failure doesn't fail the session, just skips compaction.

**Bash Output Truncation**: Default 100KB, hard limit implicitly very large. Bash stderr captured separately from stdout, both included in tool output.

## Harness-design lessons

- Separate concerns via client/server: server handles state+AI+files, clients handle UI. Enables remote scenarios and multi-client concurrency without per-UI state bloat. Use HTTP/SSE/WS, not RPC-into-UI.
- Store conversation history as file-based JSON (not SQLite): transparency, git-durable, easy migrations. Per-message + per-part granularity allows incremental streaming and tool result interleaving without full message re-reads.
- Use descending IDs for sessions (newest=smallest), ascending for messages (chronological). Enables efficient 'get latest session' queries and natural message ordering without sorting passes.
- Implement two-phase compaction: prune old tool outputs first (tokens preserved), then AI-summarize if still over. Avoids expensive summarization when pruning suffices. Retry summarization 10x with backoff (compaction failure ≠ session failure).
- Make permissions declarative per-tool per-agent: allow/deny/ask levels, bash has per-command granularity. Avoid one global permission mode; let users/agents define policy. Cache approvals to reduce friction without sacrificing safety.
- Assemble prompts from multiple sources (system > files > history > LSP > tools) in priority order, truncate low-priority context first. Keeps high-signal recent context within token limits. Separate system prompt from environment info layers.
- Use advisory locks (AbortController-based) not file locks for session concurrency. Detect (throw LockedError) instead of wait. Pair with Symbol.dispose for automatic cleanup in scoped blocks.
- Inject live environment info (working dir, git status, platform, date) at prompt time, not config time. Let AI reason about current state (e.g., 'today is Jan 15').
- Integrate LSP per-language on-demand, auto-download missing servers to ~/.tool-dir/. Cache results. Don't force all diagnostics; let tools (lsp-diagnostics, lsp-hover) fetch on demand to avoid prompt bloat.
- Use Zod for all external input validation: CLI args, API payloads, tool schemas. Share types between validation schema and TypeScript type via z.output<>.
- Namespace modules with static methods + event bus instead of classes. Enables tree-shaking and avoids constructor coupling. Pub-sub patterns decouple concerns (session updated → emit event → subscribers react).
- Support AGENTS.md files in ancestors (stop at first found per filename type). Don't merge multiple; first-found-wins keeps precedence clear. Supports tilde + glob expansion for flexibility.

## Quotes

- file:00-overview.md#L51 — 'Client/Server Architecture: Unique decoupled design where Server runs on your machine (handles AI, file operations, state) [and] Clients can connect from anywhere (TUI, desktop app, mobile app). Multiple clients can interact with the same server.'
- file:01-architecture.md#L271 — 'The prompt system is OpenCode's most complex module (54KB, 1766 lines), responsible for assembling all context needed for effective AI interactions: Context Assembly, System Prompts, Tool Descriptions, Message Formatting, Streaming & Response, Error Handling, Token Management.'
- file:03-session-management.md#L588 — 'Sessions use an advisory locking system to prevent concurrent modifications. Lock design: AbortController for cancellation, Symbol.dispose for automatic cleanup, Timestamp for debugging stuck locks, Force abort on instance shutdown.'
- file:03-session-management.md#L641 — 'Session Compaction: Two-phase. Phase 1 (Pruning): Remove old tool outputs, iterate backward from most recent, skip last 2 turns, continue until PRUNE_PROTECT tokens saved. Phase 2 (Summarization): AI-generated summary of non-compacted messages, retry up to 10x.'
- file:06-tool-system.md#L40 — 'Tool.Info interface: All tools implement id, init(), description, parameters (Zod schema), execute(args, ctx) returning {title, metadata, output, attachments?}. Context includes sessionID, messageID, agent, abort signal, callID for unique tracking.'
- file:14-security-permissions.md#L26 — 'Permission levels: allow (execute without asking), deny (reject automatically), ask (require user approval). Granular control: Per-tool, per-command (bash: {"npm test": "allow", "rm -rf": "deny"}).'
- file:26-resource-memory-management.md#L44 — 'State Disposal Pattern: Per-instance state cleanup via State.create(root, init, dispose?). Disposal called when project closes via State.dispose(projectKey). Automatic cleanup of connections, caches, LSP servers, file watchers.'
- file:04-prompt-processing.md#L262 — 'Custom instructions discovery stops at first match per file type. Order: Local AGENTS.md → parent AGENTS.md → ~/.opencode/AGENTS.md → ~/.claude/CLAUDE.md → config.instructions glob patterns. No merging; first-found-wins for precedence clarity.'

Related: [[harness]], [[harness-best-of-breed]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]].
