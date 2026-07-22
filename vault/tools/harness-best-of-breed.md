---
type: reference
tags: [tools, agents, harness]
created: 2026-07-21
updated: 2026-07-21
---

# Best-of-Breed Harness Spec (open-docs synthesis)

Opinionated stack picks for building the best CLI-agent harness, synthesized from a 2026-07-21 mining pass over `misc/open-docs` — deep-dive documentation of 5 shipped agent CLIs (Anthropic Claude Agent SDK, OpenAI Codex CLI, Google Gemini CLI, OpenCode, Pi coding-agent).

Companion notes with the raw per-tool detail:

- [[harness-claude-agent-sdk]] — Anthropic official SDK; subprocess model, 9 hooks, 4 permission modes, skills, MCP
- [[harness-codex-cli]] — Rust; 5-layer sandbox, bounded async channels, JSONL structured events, TurnDiffTracker
- [[harness-gemini-cli]] — TS monorepo; tool registry, OpenTelemetry pipeline, VS Code IDE integration
- [[harness-opencode]] — client/server split, 6-layer prompt stack, per-command bash perms, LSP+MCP, TUI
- [[harness-pi-coding-agent]] — minimalist reference harness; JSONL tree sessions, jiti JIT TS extensions

Anchor principles note remains [[harness]] (verified cross-cutting design axioms).

## The spec

Architecture: adopt opencode's client/server split as the top-level structure — a single local server (Hono or fastify) per machine owns all session state, LSP servers, file watchers, MCP connections, and LLM calls; every UI (TUI, VS Code, web, mobile, headless CI) is a thin client connecting over HTTP + SSE + JSON-RPC. Internally, structure the server with codex_cli's bounded async channels (~16 submissions, ~256 events) between a core event loop and I/O adapters so interactive and headless modes share one code path (pi's shared-core discipline).

Sessions: pi's JSONL append-only format with tree id/parentId for branching, plus opencode's descending session IDs and ascending message IDs so 'latest session' and 'chronological messages' are both O(1). Layer opencode's Git-based file snapshots on top for undo of filesystem mutations, plus claude-agent-sdk-style periodic checkpoint bookmarks inside the JSONL. Support Session.fork (opencode) and in-place tree navigation (pi). Resume must be UUID-based, not index-based — claude-agent-sdk's index-shifting-after-compaction bug is the lesson.

Prompts: opencode's 6-layer stack (provider header opt-in → provider-specific → live env info → AGENTS.md cascade first-found-wins with codex_cli's 32KB cap → agent-specific → user --system override). Inject live environment (cwd, git status, date) at prompt time. Enable automatic prompt caching (claude-agent-sdk) and surface cached_tokens (codex_cli) so users can tune.

Compaction: opencode's two-phase (prune tool outputs first, then AI-summarize) plus pi's overflow-retry (re-run the failed turn after compacting), exposed via a claude-agent-sdk-style PreCompact hook so extensions can inject custom summarization prompts.

Tools: codex_cli-style handler trait + registry, Zod schemas (opencode/gemini), decorator stack around each tool: schema-validate → permission-check → sandbox → timeout → output-truncate → emit-tool-part-events (opencode). Every handler accepts an AbortSignal + callID. Ship read/write/edit/multiedit/bash/grep/glob/ls/lsp-diagnostics/lsp-hover/patch/task/todo/webfetch. Bash: pi's truncate-to-temp-file pattern so LLMs get bounded output but users can inspect the full log.

Permissions: codex_cli's 5-layer defense-in-depth (approval policy + command allow/deny + path validation + platform sandbox + resource limits) exposed through opencode's declarative allow/deny/ask config with bash per-command granularity. Provide claude-agent-sdk's four modes (default/acceptEdits/bypassPermissions/plan) as presets over that engine, always with a canUseTool callback escape hatch. Compile in Seatbelt/Landlock — never user-configurable via YAML.

Extensibility: MCP with all four transports (claude-agent-sdk: stdio/SSE/HTTP/SDK-in-process) plus OAuth (gemini-cli). Adopt Anthropic's SKILL.md as the skills interop standard, discovered from both .opencode/skill and .claude/skills (opencode). Custom TS tools loaded via jiti (pi) — no build step. Nine lifecycle hooks (claude-agent-sdk) implemented as event-driven observers (pi) so multiple listeners coexist and one failure doesn't crash the harness. Subagents via AgentDefinition (claude-agent-sdk) for context isolation.

Configuration: 6-scope precedence — CLI > session > local > project > user > policy (claude-agent-sdk) — in JSONC (opencode) with deep merge and an explicit '$replace' escape for arrays.

Streaming & runtime control: AsyncIterable<Message> is the primitive; interrupt / setModel / setPermissionMode / steering-input are methods on the returned handle. Support pi's steering-vs-followUp distinction. Fail loudly if a runtime-control call is made on a non-streaming query — never silently drop it.

Observability: JSON Lines structured events on stderr for every turn (codex_cli): token usage per model with cached_tokens, tool calls, per-file diffs (TurnDiffTracker), compaction, permission decisions. Ship OpenTelemetry integration (gemini-cli) as the single external pipeline — no proprietary side-channel.

## Cross-cutting themes

Themes surfaced in ≥3 of the 5 harnesses. Each is a design axis where you must pick a side.

### Process architecture: subprocess vs client/server vs in-process
*Slug:* `harness-process-architecture` — *Why it matters:* The top-level architectural choice determines everything downstream: crash isolation, multi-client support, remote scenarios, streaming semantics, and how state is owned. Each of the five tools picked a different point on this spectrum, and the tradeoffs are load-bearing.

**Per tool:** claude-agent-sdk: thin TS wrapper spawns bundled CLI as child process, JSON over stdio. codex_cli: Node.js shim launches native Rust binary; internally uses tokio + async_channel bounded queues (16 submissions / 256 events) for core-UI split within one process. gemini-cli: monorepo split of CLI (Ink/React UI) and Core (API/tools/state) as packages, single process. opencode: true client/server — one Hono HTTP server per machine, multiple clients (TUI, desktop, IDE via ACP) connect over HTTP/SSE/WS/JSON-RPC. pi: single-process layered core with three thin I/O adapters (interactive/print/RPC) over one AgentSession.

**Best bet:** Follow opencode's client/server split with pi's shared-core discipline. Run a local HTTP+SSE server that owns all session state, LSP, file watchers, and LLM calls; make every UI (TUI, VS Code, web, mobile, headless CI) a thin client. Internally structure the core with codex_cli-style bounded async channels so the same event loop drives interactive and non-interactive modes. This is the only architecture that scales to remote work, multi-client observation, and RPC without duplicating logic.

**Gotchas:**
- Subprocess wrappers (claude-agent-sdk) leak runtime-control features — interrupt/setModel/setPermissionMode only work with streaming stdio, silently fail otherwise.
- Bounded channels (codex_cli 16/256) provide backpressure but require every producer to handle send-blocked without deadlock.
- Client/server needs an auth story from day one; opencode's web console auth is under-documented, which is exactly the trap to avoid.

### Session storage, branching, and resume
*Slug:* `harness-session-storage` — *Why it matters:* Sessions are the persistence substrate for agent work. Format choice determines resume speed, forkability, crash tolerance, and whether humans can inspect/git-diff conversations.

**Per tool:** claude-agent-sdk: per-session directory in ~/.claude/sessions/{id}/ with transcript.json + checkpoints/ + hashed file-snapshots; resume, forkSession (undocumented), resumeSessionAt message-UUID. codex_cli: one JSON per conversation in ~/.codex/sessions/{id}.json, plus TurnDiffTracker events and 'ghost commits' for /undo. gemini-cli: SessionState with named CheckpointData; no branching/replay documented. opencode: file-per-message + folder-per-message-parts under ~/.opencode/data/…, descending session IDs (newest = smallest), ascending message IDs, Session.fork(sessionID, messageID?), Git-based file snapshots for undo. pi: JSONL append-only per session with tree id/parentId, in-place /tree navigation between branches, /fork from any entry, auto-migration v1→v3.

**Best bet:** Adopt pi's JSONL append-only + tree(id/parentId) format for conversation history because it is crash-resistant, streamable, git-friendly, and supports branching without file explosion. Layer opencode's Git-based file snapshots on top for undo of filesystem mutations (better than diff-only). Steal claude-agent-sdk's periodic checkpoints as JSONL bookmarks and copy opencode's descending session IDs / ascending message IDs so 'latest session' and 'chronological messages' are both O(1) sorts.

**Gotchas:**
- claude-agent-sdk shifts message indices after compaction — any resumeAt logic must be UUID-based, not index-based.
- opencode advisory locks throw LockedError rather than wait; clients need retry/UX for the collision case.
- JSONL migrations (pi v1→v3) must be idempotent and auto-triggered on load; don't ship a manual migration CLI.

### Context management: prompt assembly, AGENTS.md, and compaction
*Slug:* `harness-context-management` — *Why it matters:* Prompt assembly and compaction are where cost, quality, and correctness collide. All five tools converged on layered prompts plus AGENTS.md-style project docs, but the compaction strategies diverge sharply.

**Per tool:** claude-agent-sdk: three preset system prompts, customSystemPrompt/appendSystemPrompt, auto-compaction ~100K, PreCompact hook, automatic prompt caching (cache_control: ephemeral, ~90% savings). codex_cli: 3-tier prompt (base include_str! + AGENTS.md walk to git root, max 32KB + ~/.codex/prompts/*.md slash commands), EnvironmentContext (OS/shell/cwd/git) injected into developer message, distinct CompactTask for o-series. gemini-cli: GEMINI.md discovery, no explicit compaction (relies on 1M window), ModelRouterService picks Pro/Flash by prompt+history+tools. opencode: 6-layer stack (provider header 'spoof' → provider-specific → env info → AGENTS.md cascade first-found-wins → agent prompt → --system override), 54KB prompt.ts, two-phase compaction (prune tool outputs first, then AI-summarize with 10x exponential backoff). pi: buildSystemPrompt() combines base + tools + skills + AGENTS.md walk + optional .pi/SYSTEM.md; /compact takes custom instructions; compaction retries the turn if overflow was the cause.

**Best bet:** Use opencode's 6-layer prompt stack (system → provider header → env info → AGENTS.md → agent → override) with codex_cli's 32KB cap on AGENTS.md concat. For compaction, combine opencode's two-phase design (prune tool outputs before summarizing) with pi's overflow-retry (re-run the failed turn after compacting) and expose it via a PreCompact-style hook (claude-agent-sdk) so extensions can inject custom summarization. Inject live environment info (opencode: cwd, git status, date) at prompt time, not config time.

**Gotchas:**
- opencode's provider prompt 'spoofing' ('You are Claude...' sent to non-Anthropic models) improves behavior but is ethically and legally ambiguous — make it opt-in.
- Prompt caching is invisible unless you surface cached_tokens (codex_cli does; opencode does not).
- AGENTS.md first-found-wins (opencode) vs concat-root-to-cwd (codex_cli) is a real fork — pick one and document it; users get burned when tools silently merge or silently override.

### Tool system: registry, schema, streaming, and cancellation
*Slug:* `harness-tool-system` — *Why it matters:* Tools are the agent's hands. Their contract (schema validation, cancellation, streaming, timeouts) is what separates a demo from a production harness.

**Per tool:** claude-agent-sdk: 17 built-in tools, Zod schemas via tool() factory, hard 10-min Bash timeout, FileEdit requires unique exact-whitespace match, Grep defaults to filenames (must opt-in to content). codex_cli: Rust ToolRegistry (HashMap<name, Box<dyn ToolHandler>>) wrapped by ToolOrchestrator (approval + sandbox + 30s default / 300s max timeout / 10MB output cap), model-specific tool filtering via ToolsConfig. gemini-cli: BaseDeclarativeTool<Params, Result> with Zod parameterSchema, requiresConfirmation enum (low/med/high) evaluated at invocation. opencode: 15+ tools in registry.ts, Zod-validated, context passes AbortSignal + callID, per-command bash granularity, tool parts have status (active/completed/error) streamed to model. pi: 7 tools via factories, wrapped by extension system for pre-call/post-result interception, TypeBox schema validation, bash truncates last 2000 lines and saves full output to temp file.

**Best bet:** Model tools as codex_cli-style handler trait + registry, but define schemas with Zod (gemini/opencode/claude-agent-sdk convention — Zod is the de facto standard in TS harnesses). Every tool must accept an AbortSignal (opencode) and callID for tracing. Wrap the raw handler in a decorator stack: schema-validate → permission-check → sandbox → timeout → output-truncate → emit-tool-part-events (opencode's tool part model). Steal pi's bash-truncation-with-temp-file pattern so LLMs get bounded context but users can inspect full output.

**Gotchas:**
- Hard timeouts (claude-agent-sdk 10-min bash) frustrate long-running tasks — expose it as a config with a sane default, not a constant.
- FileEdit unique-string matching (claude-agent-sdk) is brittle with whitespace; pi's fuzzy whitespace normalization is friendlier.
- Grep defaulting to filenames-only (claude-agent-sdk) is a footgun for LLMs; default to a content mode with a filenames flag.

### Permissions, sandboxing, and safety layering
*Slug:* `harness-permissions-sandboxing` — *Why it matters:* This is where harnesses diverge most philosophically — from pi's 'no popups, use containers' to codex_cli's 5-layer defense-in-depth. Getting this wrong either enables incidents or destroys UX.

**Per tool:** claude-agent-sdk: 4 modes (default/acceptEdits/bypassPermissions/plan), custom canUseTool callback, PreToolUse hook can block/modify, allowedTools whitelist only. codex_cli: 5-layer defense — approval policies (untrusted/on-failure/on-request/never) + command safety allow/deny lists + path validation (ReadOnly/WorkspaceWrite/DangerFullAccess) + platform sandboxing (macOS Seatbelt .sbpl / Linux Landlock ABI V1) + resource limits. gemini-cli: per-tool confirmation dialogs with risk enum, optional Docker/Podman sandbox with mount config. opencode: allow/deny/ask per tool per agent, bash per-command granularity, modes (default/auto-edit/auto-approve/yolo), regex dangerous-pattern detection, working-dir validation with symlink resolution. pi: intentionally no sandboxing/prompts ('security theater'), relies on containers + extension review.

**Best bet:** Adopt codex_cli's 5-layer defense-in-depth as the structural spine but expose it through opencode's declarative allow/deny/ask config so users configure policy, not code. Provide claude-agent-sdk's four modes as presets over that policy engine, and always allow a custom canUseTool callback for fine-grained escape. Platform sandboxing (Seatbelt/Landlock) must be compiled in and unconfigurable (codex_cli's insight) — no user should be able to disable the kernel-level fence via YAML. Reject pi's 'no prompts' stance for the default profile; keep it available as 'yolo' mode for power users.

**Gotchas:**
- canUseTool callback (claude-agent-sdk) can be used to escalate permissions programmatically — audit that path.
- Regex dangerous-pattern detection (opencode) is a signal, not a fence; someone will bypass it with base64 or bash indirection.
- MCP servers inherit parent env (opencode) — a subverted MCP server sees API keys unless you scrub env explicitly.

### Extensibility: MCP, custom tools, skills, and hooks
*Slug:* `harness-extensibility` — *Why it matters:* The tools that win are the ones users can extend without forking. The four extension surfaces converged across the field are MCP servers, custom tools, Skills (SKILL.md), and lifecycle hooks.

**Per tool:** claude-agent-sdk: MCP with 4 transports (stdio/SSE/HTTP/SDK-in-process), skills from ~/.claude/skills + .claude/skills, 9 hook events (PreToolUse, PostToolUse, UserPromptSubmit, SessionStart/End, Notification, Stop, SubagentStop, PreCompact), AgentDefinition for subagents. codex_cli: MCP stdio only, custom prompts in ~/.codex/prompts/*.md, no formal hook system (approval acts as the hook). gemini-cli: MCP first-class with OAuth, hooks (pre/post-prompt/tool, on-error/start/exit) as shell commands, SkillDefinition grouping tools+prompts+resources. opencode: MCP with env interpolation, custom tools from .opencode/tool/*.ts loaded via Bun.Glob, skills from .opencode/skill + Claude-compatible .claude/skills, no explicit hook system. pi: no MCP by design ('CLI tools with READMEs' via Skills instead), event-driven extensions via jiti-JIT TypeScript from ~/.pi/agent/extensions/*.ts, extensions register tools/commands/shortcuts/flags and can block tool calls.

**Best bet:** MCP is table stakes — support all four transports like claude-agent-sdk, with OAuth like gemini-cli. Adopt Anthropic's SKILL.md format as the interop standard (opencode already does this — read .claude/skills too). For hooks, use claude-agent-sdk's 9 events as the spec but implement them as pi's event-driven extension observers so multiple handlers can react and errors in one don't crash the harness. Support custom tools written in TS loaded via jiti (pi's approach eliminates build steps and dramatically improves DX). Add subagents via claude-agent-sdk's AgentDefinition pattern for context isolation.

**Gotchas:**
- MCP hook matcher syntax is undocumented in claude-agent-sdk — use callback logic instead of relying on string matchers.
- Hook shell commands (gemini-cli) with unspecified sync/async semantics create race conditions; commit to one.
- Extensions with module-level action calls (pi) will crash with 'runtime not initialized' — only allow actions inside handlers.

### Configuration layering and discovery
*Slug:* `harness-configuration` — *Why it matters:* Every tool implements a config cascade, but the number of levels and merge semantics vary enough that they cause real confusion for users moving between them.

**Per tool:** claude-agent-sdk: 6 scopes — CLI flags > session > local (.claude) > project (.claude in git root) > user (~/.claude) > policy (/etc). codex_cli: CLI > env vars > config file (YAML/JSON) > hardcoded defaults, providers block in ~/.codex/config.yaml. gemini-cli: settings in ~/.gemini/config.yaml, MCP server config there, experiment flags gate features. opencode: JSONC config with jsonc-parser, deep merge global > project ancestors > CLI override, env vars OPENCODE_CONFIG_CONTENT / OPENCODE_CONFIG_DIR. pi: two-level merge only — global ~/.pi/agent/settings.json + project .pi/settings.json — project overrides only specified keys.

**Best bet:** Copy claude-agent-sdk's 6-scope precedence (CLI > session > local > project > user > policy) because enterprise adoption requires the policy layer and per-session overrides are essential for scripting. Use JSONC (opencode) so users can comment their config. Deep-merge with explicit override semantics like opencode; document the merge order prominently — pi's two-level design is admirably simple but breaks down at scale.

**Gotchas:**
- Env var inheritance (codex_cli) surprises users when a shell export overrides project config silently.
- Deep merge of arrays vs replacement is the most common footgun — pick one, document it, provide an explicit '$replace' escape.
- Discovery walking up to git root (opencode/codex_cli) fails in non-git dirs; fall back to cwd.

### Streaming, cancellation, and runtime control
*Slug:* `harness-streaming-runtime-control` — *Why it matters:* Streaming isn't just a UX polish — it's the interface through which cancellation, mid-stream steering, and background monitoring work. Batch-response APIs preclude entire categories of features.

**Per tool:** claude-agent-sdk: AsyncGenerator<Message> as the default; runtime control methods (interrupt, setPermissionMode, setModel) only work when streaming stdio is used. codex_cli: reqwest bytes_stream().eventsource() parses SSE, deltas emitted as Event::AgentMessageDelta over rx_event channel; PTY-based shell streams char-by-char. gemini-cli: streaming via AsyncGenerator through GeminiChat. opencode: Vercel AI SDK streamText + SSE endpoints, tool result parts stream back to model with delta support. pi: sequential turn-taking with steering vs followUp queue modes for interrupting-vs-appending user input mid-stream.

**Best bet:** AsyncGenerator/AsyncIterable message stream is the right primitive (claude-agent-sdk, opencode). Every mutation the caller might want mid-stream — cancel, change model, change permission mode, add steering input — must be a method on the returned handle, not a separate API. Model pi's steering vs followUp distinction (interrupt-and-replace vs queue-and-continue) as first-class modes. Use codex_cli's bounded async channels internally so the streaming layer has real backpressure.

**Gotchas:**
- claude-agent-sdk's runtime control methods 'likely fail silently' on non-streaming queries — never ship that; error loudly instead.
- SSE reconnection semantics are under-specified across all five tools; define your resume-from-last-event-id contract early.
- Char-by-char PTY streaming (codex_cli) hits terminal escape-sequence edge cases; buffer to line boundaries when rendering.

### Observability: token usage, diffs, and telemetry
*Slug:* `harness-observability` — *Why it matters:* Observability is what turns a black-box agent into an inspectable system. Tokens, per-turn diffs, and structured events are the minimum bar.

**Per tool:** claude-agent-sdk: per-model token/cost breakdown in modelUsage map on result message, DEBUG env with subsystem filters (claude:api/cache/stream). codex_cli: TokenUsage events after every turn (input/output/cached), TurnDiffTracker emits TurnDiffEvent with per-file diffs, ThreadEvent JSON Lines on stdout in exec mode. gemini-cli: dual telemetry to OpenTelemetry (traces/metrics to GCP CloudTrace + CloudMonitoring) AND proprietary Clearcut analytics; no cache-hit metrics. opencode: token tracking (input/output/cache.read) with per-model cost, no explicit cache-hit metrics but SSE stream is inherently observable. pi: session JSONL is the audit log; context usage tracked via most-recent assistant message + trailing estimate (no per-turn tokenization call).

**Best bet:** Emit codex_cli-style structured events (JSON Lines to stderr or a dedicated pipe) for every turn: token usage per model, tool calls, per-file diffs, compaction events, permission decisions. Ship OpenTelemetry integration (gemini-cli) but skip a proprietary analytics side-channel — one pipeline. Surface cached_tokens explicitly (codex_cli) so users can measure their prompt-caching hit rate. Make JSONL session files the primary audit log (pi) and derive dashboards from them.

**Gotchas:**
- Dual telemetry (gemini-cli: OTel + Clearcut) doubles maintenance and confuses users about what's collected.
- Prompt caching is opaque unless cached_tokens is surfaced — users can't tune what they can't see.
- Per-file diff tracking (codex_cli TurnDiffTracker) requires deterministic file-write hooks; retrofitting is painful.

## Quick wins (low-cost, high-leverage)

- AsyncIterable<Message> as the primary API surface (claude-agent-sdk, opencode) — enables streaming, cancellation, and runtime control from day one.
- Zod schemas for every tool, config, and API payload (gemini-cli, opencode, claude-agent-sdk) — runtime validation plus TS types for free.
- AGENTS.md discovery walking up to git root (codex_cli, opencode, pi) — universal project-context convention, near-zero implementation cost.
- Automatic prompt caching for system prompts + AGENTS.md, and surface cached_tokens (claude-agent-sdk, codex_cli) — ~90% cost reduction with almost no code.
- Per-turn structured events as JSON Lines on stderr (codex_cli) — token usage, tool calls, diffs, permissions; makes the whole system observable and testable.
- Bounded async channels between core and I/O (codex_cli) — natural backpressure, cleaner cancellation, no runaway buffering.
- Descending session IDs + ascending message IDs (opencode) — trivial to implement, makes 'latest session' and 'chronological messages' O(1) forever.
- JSONL append-only sessions with tree id/parentId (pi) — crash-resistant, git-friendly, branchable, no DB needed.
- SKILL.md format compatible with .claude/skills (opencode) — free interop with the wider Claude ecosystem.
- JIT TypeScript extensions via jiti (pi) — no build step for user extensions dramatically improves adoption.
- Per-command bash permission granularity (opencode: {'npm test': 'allow', 'rm -rf': 'deny'}) — right-sized security without blanket allow.
- Advisory locks via AbortController + Symbol.dispose (opencode) — modern JS resource management, no file locks, throws on collision.
- Truncate bash output to N lines but save full log to temp file (pi) — bounds LLM context without hiding info from the user.
- Inject live environment (cwd, git status, platform, date) at prompt time (opencode) — lets the model reason about current state without stale config.
- Ship platform sandboxing (Seatbelt on macOS, Landlock on Linux) compiled in, not configurable (codex_cli) — kernel fence that YAML cannot disable.

## Open questions (docs don't settle these)

- Single-process vs client/server: is the operational cost of running a local server (auth, port conflict, upgrade coordination) worth the multi-client and remote-work upside for a single-developer harness?
- AGENTS.md merge semantics: codex_cli concatenates root-to-cwd; opencode uses first-found-wins per filename. Which is less surprising in a monorepo with nested overrides?
- Provider prompt spoofing (opencode injects 'You are Claude...' into non-Anthropic model calls): quality win vs ethical/legal exposure — default off, opt-in, or never?
- Sandboxing default: codex_cli's compiled-in Seatbelt/Landlock vs pi's 'containers or nothing'. Should a v1 harness ship platform sandboxing or delegate entirely to Docker?
- MCP vs Skills as the primary extension surface: pi rejects MCP outright; everyone else embraces it. Is MCP overhead worth it for a single-user tool, or does SKILL.md + CLI tools cover 90%?
- Hooks as shell commands (gemini-cli) vs in-process TS callbacks (claude-agent-sdk, pi): sync vs async semantics, sandboxing of hook code, and error containment are all unresolved.
- Compaction trigger: token threshold (~80% of context per opencode) vs actual overflow (pi retries the failed turn) — should we run both?
- Session storage: per-message JSON files (opencode) vs one JSONL per session (pi). Which handles massive tool outputs better without inode explosion or line-length pain?
- Bounded channel sizes (codex_cli 16/256) — what's the right number for a general harness, and should they be configurable per deployment (interactive vs CI)?
- Auth story for a client/server harness: opencode's web console auth is under-documented. Local-only via unix socket + fs permissions, or first-class token auth for remote clients?

## Provenance

Workflow `open-docs-harness-mining` (2026-07-21): 5 parallel Explore agents (one per docs subdir) → JSON reports → 1 synthesis pass. Single-pass, not adversarially verified. `misc/open-docs` itself is community-extracted from public source; verify any load-bearing claim against upstream before shipping.

Related: [[harness]], [[orchestrator-worker-protocol]], [[mcp-vs-native-agent-tooling]], [[model-task-benchmarks]].
