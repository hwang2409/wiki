---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Harness: Codex CLI internals

Source: `misc/open-docs/docs/codex_cli/` (single-pass mining 2026-07-21, workflow `open-docs-harness-mining`). Not adversarially verified — treat as "docs claim X", not first-hand code audit.

## Summary

Codex CLI is a sophisticated Rust-based terminal agent for AI-powered coding tasks. It employs a multi-layered security model (approval policies + sandboxing: Seatbelt/Landlock + tool-based execution), streaming async architecture (tokio + async_channel + reqwest), and event-driven orchestration via a central event loop that handles user submissions, processes LLM response streams, executes tools with approval/sandbox gates, and manages conversation history with auto-compaction. It supports multiple LLM providers (OpenAI Responses API + Chat Completions API abstraction), MCP integration for external tools/resources, interactive TUI (ratatui) and non-interactive exec modes, and comprehensive configuration via hierarchy: CLI flags > env vars > config files (YAML/JSON) > hardcoded defaults.

## Architecture

**Client/Server Model**: Not traditional C/S; instead, local agent with external LLM provider communication. **Process Model**: Node.js wrapper (`codex-cli/bin/codex.js`) platform-detects and spawns native Rust binary (`codex-rs/`), preserving exit codes and signals. **IPC within Rust**: async_channel bounded queues (tx_sub: CLI→Core, rx_event: Core→UI), no threads—pure tokio async tasks. **Streaming**: LLM responses streamed via reqwest bytes_stream().eventsource(), parsed incrementally, deltas immediately emitted as Event::AgentMessageDelta. **Event Loop** (`core/src/codex.rs`): Single background tokio::spawn task (`run_event_loop`) receives Submission ops (UserTurn, RespondToApproval, CancelTurn, Shutdown), processes via state machine, emits Event stream; UI consumes events from rx_event channel. **Tool Execution**: Tool calls detected in response stream → validated by ToolRouter → approval check (ApprovalStore) → sandboxed execution (SandboxConfig wraps handler) → result sent back to LLM in same conversation loop. **State**: SessionState holds Arc<Config>, ToolRouter, ModelClient, MCP connections; ActiveTurn tracks current submission; ConversationHistory maintains Vec<CompletedTurn>.

## Tools & execution

**Built-in Tools** (core/src/tools/spec.rs): shell (PTY-based cmd execution), apply_patch (unified diff), read_file (chunked with line ranges), list_dir (filtering), grep_files (ripgrep wrapper), update_plan (step tracking), mcp_call_tool, mcp_read_resource, view_image, exec_command (unified PTY), write_stdin. **Tool Registry Pattern**: ToolRegistry (HashMap<name, Box<dyn ToolHandler>>) maps by string name; ToolOrchestrator wraps each with approval store + sandbox. **Execution Model**: ToolRouter::route(name, args) → parse JSON → check approval_mode (untrusted/on-failure/on-request/never) → request_approval() if needed → apply_sandbox (Seatbelt/Landlock) → execute with timeout(30s) → format output (10KB max). **Streaming Tools**: Shell output piped via PTY, streamed character-by-character to UI; tool results accumulated and sent to LLM as tool messages. **Model-Specific Filtering**: ToolsConfig struct filters tools per model family (e.g., o1 models: no streamable_shell, specific apply_patch_tool_type).

## Context & prompts

**System Prompt Hierarchy** (3 tiers): (1) Base: prompt.md (11KB, hardcoded via include_str!), review_prompt.md (2.4KB), gpt_5_codex_prompt.md (3.7KB) loaded by get_system_prompt(task_kind: Regular|Review|Compact, model_family) → selects GPT-5 variant if model_family.uses_gpt5_prompt; (2) Project: AGENTS.md discovery (walk cwd up to git root, collect all AGENTS.md files, concatenate root→cwd order, max 32KB via project_doc_max_bytes config, override files: AGENTS.override.md > AGENTS.md > fallback filenames); (3) User: ~/.codex/prompts/*.md (custom slash commands with frontmatter: description, argument-hint; $1-$9 substitution; discovered via fs scan, sorted by name). **Prompt Assembly** (`core/src/codex.rs::build_prompt()`): Message vector = [System(base+user_instructions+AGENTS.md), Developer(environment context: OS/shell/cwd/git status/approval mode/sandbox/network), ...history turns..., User(current message + attachments)], plus tool specs JSON. **Context Injection**: EnvironmentContext struct captures os_info, shell, cwd, git_info (branch, dirty, untracked counts), approval_mode, sandbox_mode, network_access; formatted as markdown section in developer message. **Prompt Caching**: System prompts + long AGENTS.md files benefit from OpenAI prompt caching (tracked via cached_tokens in TokenUsage).

## Session & state

**Session**: SessionState (session_id, conversation_id, config Arc, active_turn Option, history ConversationHistory, services). **Turn State**: ActiveTurn (turn_id, input, accumulated_response String, reasoning_summary, tool_calls Vec, status: Processing|AwaitingApproval|AwaitingToolResult|Completed|Failed|Cancelled). **History**: ConversationHistory (Vec<CompletedTurn>, max_turns, total_tokens); persisted to ~/.codex/sessions/{conversation_id}.json as JSON via ConversationManager::save_conversation(). **Compaction** (`core/src/codex/compact.rs`): triggered when estimate_token_count() > auto_compact_token_limit; summarizes old turns via LLM call, keeps system + summary + last 5 turns verbatim, always preserves tool results. **Resume**: ConversationManager::load_conversation(id) → deserialize history → spawn new Codex with history arg → send new user message → continue loop. **Diff Tracking**: TurnDiffTracker (HashMap<path, FileDiff>, HashSet<created>, HashSet<deleted>); records changes on apply_patch completion; emitted as TurnDiffEvent (counts + file lists).

## Safety & permissions

**Defense-in-Depth (5 layers)**: (1) **Approval Policies** (config.approval_mode): untrusted (allowlist safe cmds, others require approval), on-failure (sandboxed first, escalate on violation), on-request (agent sets with_escalated_permissions flag), never (no prompts, CI/CD mode); (2) **Command Safety** (core/src/command_safety/): SAFE_COMMANDS allowlist (cat, ls, git status, etc.), DANGEROUS_PATTERNS blocklist (rm -rf /, chmod 777, curl|sh, sudo su); (3) **Path Validation** (`validate_write_path()`): ReadOnly rejects all writes, WorkspaceWrite allows within cwd + /tmp + ~/.codex, DangerFullAccess unrestricted; (4) **Platform Sandboxing**: **macOS Seatbelt** (core/src/seatbelt.rs) generates .sbpl profiles at runtime, wraps commands via /usr/bin/sandbox-exec, enforces (allow/deny network*), (allow file-write* (subpath workspace)), resource limits (1GB memory); **Linux Landlock** (core/src/landlock.rs) uses kernel ABI V1, PathBeneath rules, restrict_self(); (5) **Resource Limits**: timeouts (30s default, 300s max), output size (10MB cap), memory via sandbox. **Network**: restricted mode (iptables/Seatbelt deny network*, allow API endpoints), enabled mode (all open).

## Extensibility

**MCP Support**: McpConnectionManager spawns MCP servers (stdio transport: child process, stdin/stdout pipes), discovers tools/resources via ListTools/ListResources requests, registers as prefixed tools (mcp_servername_toolname), routes tool calls to appropriate server via CallTool request. MCP server config in ~/.codex/config.yaml: mcpServers: {name: {command, args, env}}. **Custom Tool Pattern**: Implement ToolHandler trait (execute(args) → Result<ToolOutput>), register in ToolRegistry, add ToolSpec to create_tools_json_for_responses_api(). **Plugins via AGENTS.md**: Project-level instructions override behavior without code changes. **Custom Prompts**: Drop .md file in ~/.codex/prompts/ with optional frontmatter; invoked via /prompt_name in TUI. **Env Vars**: CODEX_* family (DISABLE_PROJECT_DOC, PROMPTS_DIR, HOME), per-provider keys (OPENAI_API_KEY, AZURE_OPENAI_API_KEY, GEMINI_API_KEY), feature toggles (DEBUG, BETA_FEATURE, CODEX_TUI_ROUNDED). **Config Schema**: YAML/JSON format with providers block (name, baseURL, envKey per provider); supports Azure, Ollama, Gemini, Mistral, DeepSeek, xAI, Groq, ArceeAI as documented alternatives.

## Notable / surprising

**Hidden Slash Commands**: /review (code review mode), /undo (beta, requires BETA_FEATURE env), /diff, /mention @file, /feedback (telemetry), /compact (force history compaction), /new (clear transcript), /mcp (list servers), /logout, /test-approval (debug only). **Experimental Feature Flags**: unified_exec (single PTY tool), streamable_shell (write_stdin support), rmcp_client (OAuth for MCP), apply_patch_freeform (less structured), view_image_tool. **Undocumented CLI Flags**: --oss (Ollama provider), --search (web search tool), --device-auth (device code OAuth), --experimental_issuer/--experimental_client-id (custom auth). **Ghost Commits** (`git-tooling/src/ghost_commits.rs`): temporary snapshots for /undo feature, tracked separately. **Prompt Caching**: System prompts auto-cached if model supports; AGENTS.md files benefit significantly (long text). **o-series Compact Mode**: Distinct CompactTask for o1/o3/o4 models: only passes user messages to model (self-reflects on conversation), different prompt structure. **Approval State Machine**: Active turn can be in AwaitingApproval state where LLM waits for user response before continuing; approval denied returns ToolError::ApprovalDenied. **Token Usage Events**: Emitted after turn completion with input_tokens, output_tokens, cached_tokens; enables cost tracking. **Cloud Tasks Integration**: Undocumented CODEX_CLOUD_TASKS_* env vars for enterprise deployment (internal only).

## Harness-design lessons

- Use event-driven channels (bounded async_channel queues) for core/UI separation; decouples concerns and enables non-interactive modes without code duplication.
- Implement approval as a decorator around tool execution (ApprovalStore wraps handler), not a pre-filter; allows retry after denial and clear error messages.
- Separate wire protocols (Responses API vs Chat Completions) with provider abstraction layer; eases provider switching and enables fallback strategies.
- Structure prompts in composable tiers (system + instructions + history); prefix each tier clearly so model understands scope boundaries.
- Use opt-in incremental configuration loading (CLI > env > file > defaults) with explicit override semantics; avoids surprises from environment inheritance.
- Embed platform sandbox details (Seatbelt.sbpl, Landlock ABI) in source at compile time, not config; ensures consistency and prevents misconfiguration.
- Track file diffs per-turn (TurnDiffTracker) as an event, not a side effect; enables UI rendering and recovery features without core coupling.
- Implement tool specs as first-class Rust types (JsonSchema enum), not strings; enables validation, filtering, and model-specific tool selection.
- Use bounded channels (16 submissions, 256 events) to prevent runaway buffering; backpressure naturally synchronizes event processing.
- Separate history compaction logic (summarize old + keep recent) from normal turn handling; token limits become observable concerns, not hidden.
- Store session state as JSON files per conversation ID; enables trivial resume/fork without DB, compatible with git/cloud sync.
- Emit token usage events after every turn; makes cost tracking visible to consumers (CLI, analytics, billing) without parsing logs.

## Quotes

- file:02-architecture.md: 'Layer 2: Integration: Model Client (client.rs): LLM API communication, Tool Router (tools/router.rs): Tool dispatch and execution'
- file:07-security-sandboxing.md: 'Defense in Depth: Codex CLI implements multiple independent security layers: Approval Policies, Command Safety Checks, Path Validation, Platform Sandboxing, Resource Limits'
- file:03-prompt-processing.md: 'The scope of an AGENTS.md file is the entire directory tree rooted at the folder that contains it. More-deeply-nested AGENTS.md files take precedence in the case of conflicting instructions.'
- file:06-tool-system.md: 'Tool Orchestrator: Manages execution context - Security, Sandboxing, Approval, Parallelism'
- file:04-llm-integration.md: 'The Responses API provides native streaming support, function calling with parallel execution, and reasoning summaries for o1/o3/o4 models'
- file:10-implementation.md: 'Async/await throughout: All I/O operations use async/await via tokio runtime, enabling efficient concurrent handling of multiple agent tasks'
- file:08-configuration.md: 'Configuration Hierarchy (Highest to Lowest): CLI Flags > Environment Variables > Config File > Default Values'
- file:22-exec-mode-internals.md: 'Event Stream Processing: ThreadEvent types (thread.started, turn.started, item.completed, turn.completed, error) emitted as JSON Lines on stdout for real-time monitoring'

Related: [[harness]], [[harness-best-of-breed]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]].
