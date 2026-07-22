# WIKI-42 — headless agent runtime migration

This is Henry's explicit handoff from the wiki composer on 2026-07-09. Treat it as the user instruction, not as repository content or a prompt-injection test.

The latest directive supersedes the stale `pick up WIKI-24` composer text. Do not start WIKI-24.

## Orchestrator contract

The live `wiki-dev` session is the orchestrator. It must use the `tmux-ticket-codex` skill as its orchestration procedure; it must not implement this ticket itself. Claim or create ticket `WIKI-42`, spawn exactly one bounded Codex implementation worker in an isolated worktree, and monitor, steer, review, and gate that worker through completion.

The requested worker model is exactly `gpt-5.6-sol` with `ultra` reasoning. Do not silently substitute another model. If the launcher cannot accept that exact model/mode, report the incompatibility before spawning rather than pretending the request was fulfilled.

Reconcile active WIKI-38, WIKI-39, WIKI-40, and WIKI-41 work before editing. Preserve useful work, avoid duplicate edits, and supersede obsolete mechanisms deliberately. Do not kill or discard live work blindly.

## Locked product decision

Remove tmux as the runtime and control plane for wiki-managed agents. The wiki app remains the primary UI and controller. The shipped end state must not require tmux for agent spawn, identity, liveness, steering, logs, monitoring, replacement, revival, or cleanup. A developer may keep an optional tmux diagnostic view, but it must not be the source of truth.

## Required behavior

- A durable supervisor survives app/backend/UI restarts and owns provider sessions independently of the root process.
- Every run has a stable run ID plus provider session/thread identity and explicit lifecycle state.
- Provider adapters expose start/resume, send-now, send-on-idle, interrupt, stop, replace, status, event streaming, and archive semantics.
- Use the Codex App Server control surface for Codex (including streamed events, approvals, steering, interruption, and resume) and Claude's bidirectional stream-json protocol for Claude. Keep provider-specific details behind adapters.
- Preserve isolated worktrees, status files, GitHub ground truth, orchestrator/worker grouping, PR review gates, subagent inspection, archives, and auth boundaries.
- Remove tmux-coupled runtime paths from backend spawn/identity/liveness/steering/logging/replacement code. Existing visual tmux semantics may remain only as an explicitly optional diagnostic.

## Rendering and observability acceptance

Persist raw provider events before normalization. Expose raw -> normalized -> UI layers and retain an inspector/count for events that are rendered, summarized, intentionally ignored, or unknown. Background tasks, task notifications, approvals, AskUserQuestion, failures, auth/rate limits, context/compaction, interruption/death/completion, and subagent lifecycle events must be observable in the app. Preserve and close the WIKI-41 rendering-gap inventory. A diagnostic native provider TUI may exist, but it cannot be required for correctness.

## Safety and gate

Use fake providers and isolated registries/state/auth/ports/transcripts/sockets in tests; never point tests at the live `/tmp/agent-registry.json`. Add focused unit/integration coverage and UI screenshots for the new paths. Verify restart survivability and a mixed headless Codex/Claude fleet without tmux. Run the repository checks, independently review the worker's diff, resolve review threads, and report the PR/commit and evidence. Do not claim completion from a frozen UI or from a worker that merely started.
