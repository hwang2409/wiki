---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Harness: Gemini CLI internals

Source: `misc/open-docs/docs/gemini-cli/` (single-pass mining 2026-07-21, workflow `open-docs-harness-mining`). Not adversarially verified — treat as "docs claim X", not first-hand code audit.

## Summary

Gemini CLI is a TypeScript-based monorepo CLI agent for Google Gemini with a React/Ink-based terminal UI. It implements multi-package architecture (CLI + Core + A2A server), MCP protocol integration, tool confirmation dialogs, automatic model routing with fallbacks, OpenTelemetry-based observability, Docker/Podman sandboxing, and declarative tool system. The harness prioritizes UI/core separation for reusability, type-safe schemas via Zod for API integration, and safety-first confirmation workflows for dangerous operations.

## Architecture

Multi-package monorepo (npm workspaces) with clear separation: `packages/cli` (Ink/React UI, input processing, display) → `packages/core` (API communication, tool registry, agent orchestration, configuration). Third tier: `packages/a2a-server` (agent-to-agent protocol on port 41242), `packages/vscode-ide-companion` (VS Code extension). Tools execute via ToolRegistry which resolves both built-in (`BaseDeclarativeTool` subclasses) and MCP-discovered tools. GeminiChat class (core) manages conversation state with streaming via AsyncGenerator. Config flows top-down; telemetry/events flow bottom-up (ClearcutLogger + OpenTelemetry SDK exporting to GCP CloudTrace, CloudMonitoring). Sandbox layer (optional Docker/Podman) isolates shell commands with mount-point config. MCP protocol via `@modelcontextprotocol/sdk@^1.23.0` connects external tool servers.

## Tools & execution

15+ built-in tools: File ops (Glob, ReadFile, ReadManyFiles, Edit, LS), Search (Grep, RipGrep), Shell (ShellTool with confirmation), Web (WebFetch, WebSearch grounding), Todos (WriteTodos), Delegation (DelegateToAgent). Each tool: extends `BaseDeclarativeTool<Params, Result>`, declares static `toolName`, `description`, `parameterSchema` (Zod), implements `execute(signal: AbortSignal)` returning ToolResult. Tools marked `requiresConfirmation` (Enum: low/medium/high risk) trigger ToolCallConfirmationDetails UI before execution. Confirmation outcome: `{ approved: true } | { approved: false; reason: string }`. MCP tools dynamically discovered via `McpClientManager.discoverTools()`, wrapped as DiscoveredMCPTool, executed via `callTool(server, toolName, params)`. Shell tool enforces timeout (ms), cwd, requires user approval for high-risk commands.

## Context & prompts

System prompts injected via GeminiChat constructor; updated via `setSystemInstruction()`. Context files (GEMINI.md, per-project) provide project-specific guidance and tool preferences; located via WorkspaceContext.geminiMdPaths. No explicit context compaction; relies on 1M-token window (Gemini 3) + automatic context file inclusion. Token caching enabled via Config.cacheContextFiles (repeating context cached). Prompt templating via PromptRegistry (render by name + variables). Routing context fed to ModelRouterService: considers promptLength, historyLength, toolsRequested, userPreference to select model (Pro for complex reasoning, Flash for speed/cost). Experiments system (ExperimentFlags enum) gates features for gradual rollout without code changes.

## Session & state

Sessions checkpointed via SessionState { sessionId, history (Content[]), checkpoint, metadata }. CheckpointData { id, name?, timestamp, historyLength } persists for resume. ResumedSessionData restores full state including systemInstruction. History: Content[] where Content = { role: 'user'|'model'|'function', parts: Part[] }; Part can be text, inlineData (multimodal), functionCall, or functionResponse. Serialization: direct JSON stringify/parse for most models. Terminal output special-cased as AnsiOutput (lines of AnsiTokens with styles). Session metadata tracks createdAt, lastUpdatedAt, model, tokenCount. No transcript replay mechanism documented; checkpoints enable resume but not branching/undo.

## Safety & permissions

Tool confirmation dialogs block execution for write/destructive ops: Shell (high-risk commands), Edit, WebFetch. ToolCallConfirmationDetails shown with tool name, params, description, risk level, onConfirm callback. User can approve once or add to trusted list. Confirmation skipped for read-only tools (ReadFile, Glob, Grep). Sandbox layer (Docker/Podman, optional via GEMINI_SANDBOX env or --sandbox flag) isolates shell execution: mounts current dir as /workspace (RW), ~/.gemini as /home/gemini/.gemini (RO). Mount points configurable in SandboxConfig. No WASM sandbox or seccomp details documented. No fine-grained permission model beyond tool-level confirmation and sandbox toggle.

## Extensibility

MCP protocol (Model Context Protocol) primary extension point: define MCPServerConfig in ~/.gemini/config.yaml with command, args, env, cwd, timeout, optional OAuth (clientId, clientSecret, scopes, authorizationUrl, tokenUrl). McpClientManager discovers tools async; tools auto-registered in ToolRegistry. Custom tools: extend BaseDeclarativeTool, define parameterSchema (Zod), implement execute(). Custom commands: export CommandModule (yargs) from packages/cli/src/commands/. Skills system (SkillDefinition: name, description, version, activationCommand, tools, prompts, resources) allows grouping tools + prompts + resources; activate via skill registry. Hooks (HookDefinition) for automation: pre-prompt, post-prompt, pre-tool, post-tool, on-error, on-start, on-exit events with command + args + env + timeout. No plugin loader documented; extensions require code changes or config file edits.

## Notable / surprising

1. **Ink fork**: Uses custom fork `@jrichman/ink@6.4.7`, not upstream Ink 5.x; suggests deep customizations for CLI needs (no details on why). 2. **Plain objects mandate**: GEMINI.md explicitly prefers plain objects over classes for React integration + serialization; even config and state use interfaces, not class instances. 3. **Tool confirmation risk enum** (low/medium/high) decoupled from tool definition; risk assessed at invocation time, allowing context-aware approval UX. 4. **Model routing hidden**: ModelRouterService logic not exposed in API docs; internal decision made at request time, user only sees fallback chain (Pro → Flash → Flash-lite). 5. **Token caching automatic**: Config.cacheContextFiles enables repeating-context caching transparently; no cache-hit metrics exposed. 6. **OpenTelemetry + Clearcut dual**: Logs to both OTel (traces, metrics) and proprietary Clearcut analytics; different data pipelines. 7. **A2A server on fixed port 41242**: Agent-to-agent communication defaults to 41242, configurable via CODER_AGENT_PORT env; suggests coordination with other Google tools. 8. **No hook execution model**: Hooks defined as shell commands + env + timeout; executed synchronously/async not specified; continueOnError flag suggests hook failure is catchable. 9. **Skill activation context-aware**: Skills can have activation conditions (activationCommand?); no details on how conditions are evaluated. 10. **Workspace type detection**: Infers project type (node/python/rust/go/java) from WorkspaceContext; used in routing but no inference logic documented.

## Harness-design lessons

- Separate CLI/UI from core business logic as distinct packages; core reusability across IDE extensions and APIs justifies complexity overhead vs. monolithic design.
- Use Zod schemas for all config, tool parameters, and data structures; enables runtime validation, auto-generates types, integrates naturally with API function-calling without manual marshalling.
- Implement tool-level confirmation with risk assessment (low/medium/high) shown pre-execution; users approve dangerous ops explicitly, building trust in autonomous tool use without blanket tool allowlists.
- Design session state as plain serializable objects (Content[], CheckpointData), not class instances; enables transparent checkpoint save/resume without serialization headaches.
- Route model selection automatically based on context (prompt size, reasoning complexity, cost) without user intervention; expose decision reason to users post-hoc but don't require pre-selection.
- Support MCP protocol as first-class extension mechanism; standardized tool discovery + OAuth integration reduces friction vs. custom plugin systems.
- Emit telemetry events (session_start, tool_call, agent_finish) with structured properties; dual-channel to both OpenTelemetry (traces/metrics) and analytics backend (aggregation).
- Gate experimental features via ExperimentFlags enum + settings; disable risky features instantly without redeploy; enterprise customers can opt-in/out granularly.
- Sandbox shell execution in Docker/Podman with mount-point config (source/target/readonly); users toggle via single flag, reducing adoption friction despite added complexity.
- Build error classification (category: auth/rate_limit/network/validation/execution/internal, severity, recoverable flag) into core; surface classification to users for actionable error messages, not raw stack traces.

## Quotes

- file:01-project-overview.md: As a terminal-first AI assistant, Gemini CLI enables developers to Query and edit large codebases using natural language, Generate new applications from PDFs, images, or sketches.
- file:02-technical-stack.md: **Why Ink?** Component-based UI development with React patterns, Declarative rendering for complex terminal layouts, Hot reloading during development.
- file:03-development-setup.md: 1. **Prefer plain objects over classes** ... Prefer ES module exports for encapsulation ... Prefer `unknown` over `any`
- file:04-api-reference.md: The CLI handles OAuth flow automatically; Users are prompted to authenticate on first run.
- file:05-data-models.md: type ToolConfirmationOutcome = | { approved: true } | { approved: false; reason: string };
- file:06-deployment.md: Release cadence: nightly (daily UTC 0000), preview (weekly Tuesday UTC 2359), latest (weekly Tuesday UTC 2000 promoted from preview).
- file:07-monitoring-logging.md: **NOT Collected:** Prompt content, File contents, Personal identifiers, API keys/credentials
- file:08-design-decisions.md: Implement automatic model selection based on context and requirements ... Cost Optimization, Performance, Quality, User Experience.

Related: [[harness]], [[harness-best-of-breed]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]].
