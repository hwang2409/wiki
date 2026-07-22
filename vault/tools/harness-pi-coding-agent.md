---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Harness: Pi (coding-agent) internals

Source: `misc/open-docs/docs/coding-agent/` (single-pass mining 2026-07-21, workflow `open-docs-harness-mining`). Not adversarially verified — treat as "docs claim X", not first-hand code audit.

## Summary

Pi is a minimalist terminal-based coding agent (@mariozechner/pi-coding-agent, v0.49.2) that prioritizes transparency, extensibility, and single-user focus. Built in TypeScript, it offers read/bash/edit/write tools with a sophisticated JSONL-based session system supporting conversation branching. Three operating modes (interactive TUI, print/headless, RPC) share a common AgentSession core. No MCP, sub-agents, or background processes by design—favoring explicitness and observability.

## Architecture

Layered architecture: CLI entry layer (src/cli.ts, argument parsing) → AgentSession core orchestrator (src/core/agent-session.ts) → Agent from pi-agent-core (multi-LLM abstraction) → three mode implementations (src/modes/{interactive,print-mode,rpc}). Session persistence via SessionManager (JSONL append-only format with tree id/parentId structure). Extension system uses event-driven architecture with observer pattern; extensions loaded via jiti (JIT TypeScript compilation). Tools are factory-created (createCodingTools, createReadOnlyTools, createAllTools) and wrapped to allow extension interception. Settings use two-level merge (global ~/.pi/agent/settings.json + project .pi/settings.json). File:section refs: src/core/agent-session.ts (main orchestrator), src/core/session-manager.ts (JSONL persistence), src/core/extensions/runner.ts (event dispatch), src/core/tools/index.ts (tool factories), src/modes/ (three operating modes).

## Tools & execution

Seven built-in tools: read (text/image, 2000-line/64KB limit, line offset/limit params), bash (timeout support, output truncation, exit code), edit (exact text matching with fuzzy whitespace normalization), write (auto-mkdir), grep (ripgrep, respects .gitignore, max 100 results), find (fd tool, .gitignore aware, max 1000 results), ls (includes dotfiles, 500-entry limit). Tool result includes structured details object (truncation info, diffing, metadata). Truncation happens via truncateTail (keep recent) or truncateHead (keep start) utilities. Bash supports command prefix override (shellCommandPrefix setting). Grep/find output truncated to 2000 lines or 64KB (whichever first). Tools wrapped by extension system allowing interception pre-call (tool_call event) and post-result (tool_result event). Parallel execution not native—sequential turn-taking with steering/followUp queues.

## Context & prompts

System prompt built via buildSystemPrompt() combining: base prompt, tool descriptions, skills (from discovery), context files (AGENTS.md/.APPEND_SYSTEM.md), and optional custom system prompt (project .pi/SYSTEM.md or global ~/.pi/agent/SYSTEM.md). Context files auto-discovered walking up directory tree from cwd. Prompt templates (*.md in ~/.pi/agent/prompts or .pi/prompts/) support argument expansion ($1, $@, ${@:N}). Skills follow Agent Skills standard (SKILL.md with YAML frontmatter); loaded from ~/.pi/agent/skills/, .pi/skills/, ~/.claude/skills/, ~/.codex/skills/. No memory/skill persistence beyond session files. Compaction uses custom instructions parameter (/compact [instructions]) to control summarization. Thinking levels (off/minimal/low/medium/high/xhigh) model-specific; managed via setThinkingLevel(). No hooks in traditional sense—event-based extension interception only (session_before_compact, before_agent_start, tool_call, etc.).

## Session & state

Sessions stored as JSONL at ~/.pi/agent/sessions/<project-path>/*.jsonl. Append-only format: header line (type:session, version:3, id, timestamp, cwd) followed by entry lines. Entry types: message (user/assistant/toolResult), thinking_level_change, model_change, compaction (summary + firstKeptEntryId + tokensBefore), branch_summary, custom, custom_message, label, session_info. Tree structure via id/parentId fields (null for root). SessionManager tracks current leaf (active branch). Sessions auto-resume via /resume or -c flag. Forking creates new session file with parentSession link. /tree command enables in-place navigation between branches. Entries support labels for bookmarking. Compaction creates CompactionEntry, removes older messages, optionally retries turn if overflow occurred. No transaction support—JSONL append-only semantics. Session metadata: created/modified timestamps, first message text (for session picker). Migration from v1→v3 auto-triggered on load.

## Safety & permissions

No sandboxing or permission prompts by design (intentional anti-pattern per philosophy). Bash execution in cwd with configurable shell (Windows: custom path, Git Bash, bash.exe on PATH). File operations scoped to absolute paths or cwd-relative. Credentials (auth.json) use file locking (proper-lockfile) for concurrent access; permissions recommended 600. API keys resolved via priority: runtime override → auth.json → env vars → fallback resolver. Extensions can block tool execution via tool_call handler returning {block: true, reason}. No user-facing permission UI. Security model relies on: container isolation (if needed), extension code review, filesystem permissions, and auth.json protection. Auth storage supports both API keys and OAuth (auto-refresh, file-locked). No built-in rate limiting or request throttling. Bash runs with -c flag (non-interactive) unless shellCommandPrefix override applied.

## Extensibility

Extension system: TypeScript files loaded via jiti from ~/.pi/agent/extensions/*.ts, .pi/extensions/*.ts, or --extension CLI flag. Export default function(pi: ExtensionAPI). No build step required. Extensions declare event handlers (pi.on(eventType, handler)), custom tools (pi.registerTool()), commands (pi.registerCommand()), shortcuts (pi.registerShortcut()), flags (pi.registerFlag()). Event types: session_* (session_start, session_before_compact, etc.), agent_* (agent_start, before_agent_start, etc.), tool_* (tool_call, tool_result), input, model_select. Extensions can't crash core (error handlers catch/log). Action methods (sendMessage, setLabel, etc.) only work from handlers, not module-level (throw "Extension runtime not initialized"). Tools/commands receive ExtensionContext with access to sessionManager, modelRegistry, cwd, model, UI context (theme, dialogs, widgets, status bar). RPC mode accessible via RpcClient SDK class for external integrations. Custom models via ~/.pi/agent/models.json (supports openai-completions, anthropic-messages, google-generative-ai, bedrock, etc.). Model API key resolution: literal, env var, or !shell command. Skills discoverable across codex/claude/pi paths; loadable on-demand by agent.

## Notable / surprising

No MCP support—intentional design choice favoring CLI tools with READMEs (Skills) for simpler debugging/transparency. No sub-agents, permission popups, plan mode, built-in todos, or background bash execution—all viewed as anti-patterns or context bloat. Compaction retries turn after overflow—not just summarize, but retry failed request if context was issue. Session tree navigation is in-place via currentLeaf pointer, not separate files. Extensions can provide custom compaction logic via session_before_compact hook returning {compaction: {...}}. Thinking levels model-specific; levelof.cycleThinkingLevel() auto-wraps around available levels. Auto-image resize to 2000x2000 (disableable). JSONL v3 format auto-migrates v1/v2 on load. Context usage tracked via most-recent assistant message usage + trailing token estimates (no tokenization API call per turn). Steering mode vs followUp mode (all vs one-at-a-time delivery). Compaction decision flow includes threshold vs overflow triggers with different retry behavior. Bash tool truncates last 2000 lines (not first), saves full output to temp file if truncated. Double-escape action configurable (fork vs tree). Editor padding (editorPaddingX), hardware cursor toggle, markdown code block indent customizable.

## Harness-design lessons

- Use JSONL append-only format for session storage; enables crash-resistance and git-friendly history tracking without database/transaction overhead. Pair with tree id/parentId for branching without file multiplication.
- Build extension system on event-driven observer pattern, not imperative hooks. Let multiple extensions listen to same event; catch handler errors to prevent one extension breaking others.
- Separate operating modes (interactive/headless/RPC) by implementing thin I/O adapters over shared AgentSession core, not three separate implementations. Ensures feature parity and eases maintenance.
- Design compaction as AI-powered summarization with custom instructions and overflow retry, not simple truncation. Capture intent/decisions not just facts to preserve context continuity across long conversations.
- Prioritize explicit tool execution (read/bash/edit/write as LLM-callable) over implicit file watching. Gives agent and user full visibility into what's happening; easier to debug and extend.
- Load extensions dynamically via JIT TypeScript (jiti) to eliminate build step and enable per-extension dependencies. Users write TypeScript, run immediately—improves DX and adoption.
- Use two-level settings merge (global + project) not config cascades. Project overrides only specified keys, preserving global defaults. Enables team sharing via git without breaking local preferences.
- No background processes, permission prompts, or sub-agent orchestration in core. Keep single-user focus explicit; users can build advanced patterns via tmux + extensions if needed.
- Validate tool parameters at runtime via schema (TypeBox) not just TypeScript types. Provides error messages for invalid LLM inputs and clear JSON Schema for model understanding.
- Make session branching UX straightforward via /tree in-place navigation and /fork from any point. Avoid separate files per branch; single source of truth reduces cognitive load.

## Quotes

- file:01-project-overview.md: 'No MCP - Build CLI tools with READMEs instead (Skills system). No sub-agents - Spawn pi instances via tmux for observability. No permission popups - Security theater; use containers or extensions.'
- file:02-technical-stack.md: 'JSONL Session Format - Human-readable, Append-only, Streamable, Version control friendly, Tree structure, Simple format, Crash-resistant.'
- file:04-api-reference.md: 'subscribeAgentSessionEvent types include auto_compaction_start/end and auto_retry_start/end events; extensions can listen and react to context management lifecycle.'
- file:05-data-models.md: 'CompactionEntry includes summary, firstKeptEntryId, tokensBefore, and optional details for extension-provided metadata; allows custom compaction strategies via hooks.'
- file:07-monitoring-logging.md: 'OAuth tokens are automatically refreshed when expired. File locking ensures safe concurrent access during refresh operations in auth.json.'
- file:08-design-decisions.md: 'Extensions use event-driven architecture with sequential handler execution and result merging for session_before_* events; extensions can cancel, block, or provide custom implementations.'
- file:03-development-setup.md: 'Bash runs in non-interactive mode (bash -c), which doesn't expand aliases by default. Enable via shellCommandPrefix setting in settings.json.'
- file:06-deployment.md: 'API key resolution: Direct value, environment variable name, or command execution prefixed with ! (e.g., !vault read -field=api_key secret/my-api); shell results cached for performance.'

Related: [[harness]], [[harness-best-of-breed]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]].
