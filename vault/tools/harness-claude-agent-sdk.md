---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Harness: Claude Agent SDK internals

Source: `misc/open-docs/docs/claude-agent-sdk/` (single-pass mining 2026-07-21, workflow `open-docs-harness-mining`). Not adversarially verified — treat as "docs claim X", not first-hand code audit.

## Summary

The Claude Agent SDK is a TypeScript/JavaScript library enabling programmatic creation of AI agents with Claude Code capabilities. It uses a subprocess architecture where an SDK wrapper (v0.1.22) spawns a bundled CLI executable (v2.0.22) and communicates via JSON streaming over stdio. The SDK provides 17 built-in tools (file ops, bash execution, web, MCP), 9 lifecycle hooks for behavioral interception, 4 permission modes with fine-grained control, 4 MCP transport types (stdio, SSE, HTTP, SDK in-process), session management with resumption/forking, and a specialized multi-agent system with context isolation. It includes sophisticated prompt caching, runtime control APIs, and extensive configuration layering across 6 scopes.

## Architecture

Parent process spawns child process. User application imports @anthropic-ai/claude-agent-sdk (521KB compiled TypeScript), which implements a thin wrapper layer in sdk.mjs. On query(), this spawns the bundled CLI executable (cli.js, 9.3MB minified JavaScript) as a child process. Parent and child communicate bidirectionally via stdio using JSON message streaming format. User can configure via AsyncGenerator interface supporting streaming input/output. CLI process is kept alive for session duration, terminated on completion. MCP servers spawned as optional additional processes. File: sdk.mjs (main SDK), cli.js (execution engine), vendor/ripgrep/ (bundled search binaries), vendor/claude-code-jetbrains-plugin/ (IDE integration JARs), yoga.wasm (layout engine).

## Tools & execution

17 built-in tools across 6 categories: FileRead (paginated, max 2000 lines, supports PDF/images), FileWrite (atomic, requires prior read), FileEdit (search-replace with uniqueness constraints), Glob/Grep (ripgrep-powered file search), NotebookEdit (Jupyter cells), Bash (10-minute hard timeout, background execution support), BashOutput/KillShell (background process management), Agent/Task (subagent delegation), TodoWrite (task management), ExitPlanMode (planning mode exit), WebFetch/WebSearch (web operations), MCP tools (ListMcpResources, ReadMcpResource, McpInput for generic MCP call). Tool definition via `tool()` function (name, description, Zod schema, async handler). Tool restrictions: allowedTools (whitelist-only), disallowedTools work inversely. Bash has hardcoded 10-minute timeout. FileEdit requires exact string match (whitespace-sensitive). Grep returns filenames by default unless output_mode explicitly set to "content".

## Context & prompts

Three system prompts selected by mode: "You are Claude Code, Anthropic's official CLI for Claude" (default), "...running within the Claude Agent SDK" (SDK mode), "You are a Claude agent, built on Anthropic's Claude Agent SDK" (agent mode). Custom prompts via customSystemPrompt (replace) or appendSystemPrompt (append). Preset system prompt mode available: `systemPrompt: { type: 'preset', preset: 'claude_code', append?: string }`. Specialized prompts for task agents (Explore, Security, CodeReview, etc.). Conversation auto-compacted beyond ~100K tokens. Prompt caching enabled automatically (cache_control: ephemeral) for system prompts and repeated context (90% cost reduction). Hook system allows context injection at 9 lifecycle points: PreToolUse, PostToolUse, UserPromptSubmit, SessionStart/End, Notification, Stop, SubagentStop, PreCompact.

## Session & state

Sessions stored in ~/.claude/sessions/{session-id}/. Session structure: transcript.json (full message history), checkpoints/ (auto-saved snapshots), file-snapshots/ (hashed file contents). Session ID is UUID generated at startup. Resume via `resume: session-id` option (restores full history). Fork via `forkSession: true` (creates new session from existing history). Resume from specific message via `resumeSessionAt: message-uuid`. Checkpoints created at regular intervals (every 5 turns estimated). Conversation compaction automatic on size; manual via PreCompact hook. Message indices shift post-compaction (gotcha). Session lifecycle tracked via SessionStart/SessionEnd hooks with exit reasons (success, error_max_turns, error_during_execution, interrupt, user_stopped). No cross-session memory; each new session starts fresh unless explicitly resumed.

## Safety & permissions

4 permission modes: 'default' (ask for sensitive ops), 'acceptEdits' (auto-accept file edits), 'bypassPermissions' (no prompts), 'plan' (planning only, no execution). Permission updates at runtime via setPermissionMode(). Permission rules stored per tool. Custom canUseTool callback allows fine-grained permission logic. Permission denials returned in result message. Hook PreToolUse can intercept and block/modify tool calls. Hooks run with sandbox context (session_id, transcript_path, cwd, permission_mode). MCP server approval system for .mcp.json files (project-level): enabledMcpjsonServers, disabledMcpjsonServers, enableAllProjectMcpServers boolean. First-time approval prompts when unknown .mcp.json server detected. Tool whitelisting via allowedTools (only these allowed); no inverse blacklist alone. Path filtering ignores node_modules, .git, .svn, .hg, dist, build, .cache by default.

## Extensibility

MCP support (Model Context Protocol) with 4 transport types: stdio (external process), SSE (Server-Sent Events HTTP stream), HTTP (standard REST), SDK (in-process TypeScript). Define custom tools via `tool(name, description, Zod schema, handler)` and `createSdkMcpServer({ name, version, tools })`. MCP tools seamlessly integrated alongside built-in tools. Skills system (Markdown-based SKILL.md files) with frontmatter: name, description, agent type (Explore, Security, etc.), allowed-tools list, when_to_use. Skills discovered from ~/.claude/skills (user), .claude/skills (project), local .claude/skills (gitignored). Template variables via {{$ARGUMENTS}} in skill prompt. Agents customizable with AgentDefinition: description, tools (restrict), prompt (custom system prompt), model (inherit or specify). Hook system extensible via Partial<Record<HookEvent, HookCallbackMatcher[]>>. Custom permission logic via CanUseTool callback.

## Notable / surprising

Hook matcher syntax undocumented (use callback logic instead of matchers). Conversation indices shift post-compaction (gotcha for resuming at message UUID). Runtime control methods (interrupt, setPermissionMode, setModel) only work with streaming input/output. Permission escalation possible programmatically (can bypass all security via canUseTool callback). FileEdit requires unique string match (no regex, exact whitespace). Grep returns filenames by default (must specify output_mode: "content" for content). Bash output cached per session (rerun with different output_mode to refresh). Message replay tracking (isReplay flag prevents duplicates). Synthetic user messages (isSynthetic flag marks system-generated prompts). Resume from specific message requires message.message.id (not obvious structure). Session forking (forkSession) not documented in official docs. Permission prompt tool name customizable (undocumented). Strict MCP config mode enforces validation (strictMcpConfig: true). Executable runtime selection (bun/deno/node) undocumented. Custom Claude Code executable path (pathToClaudeCodeExecutable) for testing. AccountInfo introspection available but fields optional. USE_BUILTIN_RIPGREP env var controls ripgrep source (bundled vs system). DEBUG env var supports undocumented subsystem filters (claude:api, claude:cache, claude:stream). ANTHROPIC_API_URL override for staging/proxy testing. Prompt caching automatically enabled (cache_control: ephemeral)—calls cost 90% less on cache hits.

## Harness-design lessons

- Subprocess isolation beats in-process execution: use a child process boundary for CLI tools to prevent crashes from affecting parent stability and enable clean termination without resource leaks.
- Make streaming the default interaction model: AsyncGenerator<Message> is better than Promise<Result> for real-time updates, memory efficiency, and runtime cancellation vs buffering entire conversations.
- Offer four permission modes, not just boolean: 'default' (ask), 'acceptEdits' (safe auto-accept for file ops), 'bypassPermissions' (testing/automation), and 'plan' (cost-effective design phase) serve different user intent patterns.
- Implement 9 hook events covering full lifecycle, not just pre/post: SessionStart, SessionEnd, Stop, PreCompact enable monitoring and custom logic without modifying core behavior.
- Use JSON Schema validation with Zod at runtime: type safety at compile time plus schema-based tool definitions that can be introspected, validated, and converted to MCP protocol format.
- Support 4 MCP transport types with unified interface: stdio (external CLIs), SSE/HTTP (remote servers), SDK in-process (low latency) serve different deployment scenarios from one abstraction.
- Encode configuration at 6 precedence levels: CLI flags > session > local (.claude) > project (.claude in git root) > user (~/.claude) > policy (/etc), allowing granular per-session, per-project, and enterprise overrides.
- Auto-compact conversations beyond ~100K tokens transparently: maintain full context early, then summarize history to manage costs—but track pre-compaction state in metadata (trigger, token count) so hooks can archive.
- Track per-model token usage and cost separately: return usage breakdown by model in result message (modelUsage map) so callers understand cost distribution across Claude Haiku/Sonnet/Opus.
- Store sessions with checkpoints and file snapshots: enable resume/fork/rollback by persisting transcript.json + auto-checkpoints every 5 turns + hashed file-snapshots for efficient recovery.
- Make message indices stable post-compaction: shift indices in SDKMessage so code doesn't break when resuming after compaction—document that compaction is transparent to caller.
- Implement capability-based permission system with callback override: modes + rules + custom canUseTool(toolName, input, suggestions) callback allows fine-grained security without hardcoding all logic.
- Use stdout for messages, stderr for debug: keep debug/verbose output separate so callers can capture clean message stream without parsing noise.
- Bundle critical dependencies (ripgrep, yoga.wasm): avoid external binaries that may not be installed, ensuring deterministic behavior across platforms (macOS/Linux/Windows ARM/x64).
- Support runtime control methods (interrupt, setModel, setPermissionMode) on Query interface: allow mid-stream pivots to high-cost models, permission changes after reviewing first response, or graceful cancellation.
- Hide implementation complexity behind simple query() entry point: single function with AsyncIterable<Message> return type hides subprocess spawning, stdio communication, MCP server setup, and permission logic.

## Quotes

- file:design.md:49-76 Architecture diagram showing parent app → SDK layer → CLI process → Claude API
- file:architecture.md:285-330 CLI entry point ZP8() lazy loading pattern: 'var S = (A, B) => () => (A && (B = A(A = 0)), B)' singleton factory
- file:design.md:80-98 Process model: 'The SDK uses a subprocess architecture: 1. Parent Process: User application with SDK 2. Child Process: CLI executable spawned 3. Communication: STDIO-based JSON streaming'
- file:hooks-permissions-complete.md:41-56 All 9 hook events declared as readonly tuple: PreToolUse, PostToolUse, Notification, UserPromptSubmit, SessionStart, SessionEnd, Stop, SubagentStop, PreCompact
- file:implementation-gotchas.md:39-67 Runtime control methods gotcha: 'These control methods are only supported when streaming input/output is used. Using them with non-streaming queries will likely fail silently or throw errors.'
- file:implementation-gotchas.md:104-129 Session forking hidden feature: 'forkSession: true—When resuming a session, create a new session ID instead of continuing the previous one. This allows you to branch conversations.'
- file:types-complete.md:45-94 Options type encompasses full configuration surface: abortController, maxTurns, resume, forkSession, customSystemPrompt, allowedTools, canUseTool, mcpServers, strictMcpConfig, hooks, executable, pathToClaudeCodeExecutable
- file:plugins.md:56-68 Plugin system CLI-only distinction: 'There is NO SDK-level plugin system for programmatic use. The plugin system is CLI-only.—manifest-based JSON files, `claude plugin ...` commands, marketplace-driven discovery'

Related: [[harness]], [[harness-best-of-breed]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]].
