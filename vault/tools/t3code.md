---
type: reference
tags: [tools, agents, prior-art]
created: 2026-07-30
updated: 2026-07-30
---

# t3code (prior art)

github.com/pingdotgg/t3code — theo/ping.gg's "agent harness control surface". MIT, TypeScript, ~15.8k stars, created 2026-02-08, actively pushed (checked 2026-07-30). Homepage t3.codes. Same problem space as the wiki app's supervisor/backend: a local server owns provider CLI sessions (Claude Code, Codex, Cursor, Grok Build, OpenCode) and clients control them remotely — web, Electron desktop, native iOS/Android. `npx t3@latest` runs server + local web app install-free.

## Architecture (docs/internals/)

- Server is the sole execution boundary: every provider process, terminal, git op, filesystem read happens server-side. Clients (web/desktop/mobile) share one `client-runtime` package and talk over a single authenticated Effect RPC WebSocket (`/ws`) with per-method scope authorization — holding a socket is not authorization to call everything.
- Orchestration is EVENT-SOURCED: clients dispatch typed commands → single worker fiber totally orders them → pure decider produces events → events appended + projected into read model inside ONE SQL transaction, with durable command receipts for idempotent retries. Read model cannot durably disagree with the event log.
- Provider driver registry: 5 drivers (driverKind + config schema + adapter); orchestration layer never knows which agent is behind a thread. Adding a provider = driver + adapter only.
- Turn-bracketed CHECKPOINTING: workspace state captured as hidden git refs before/after each turn — exact per-turn diffs, revert of both workspace AND provider conversation.
- Queue-backed drainable workers (ingestion / provider-command reactor / checkpoint reactor) with transactional counts; tests await `drain` instead of sleeping.
- Stack: Effect (RPC + services), Vite+ (`vp` CLI), Node 22.16+.

## Compared to the wiki app

- Overlap: multi-provider process supervision, remote steer surface, durable session state, resume across restarts.
- t3code is thread/IDE-centric (one human driving N sessions, approvals, diffs, checkpoints). Wiki's layer above — autonomous orchestrator/worker fleets, tickets, review gates, workgraph, fleet monitors — has no counterpart there.
- Ideas worth stealing: (1) event-sourced command log w/ receipts — stronger version of workgraph request_id idempotency, would have made the [[supervisor-fingerprint-swap-wedge]] class of split-brain harder; (2) hidden-git-ref turn checkpoints for exact diff/revert; (3) per-method scope auth on one websocket for a future remote/mobile wiki surface.
- Status: self-described "very very early, expect bugs"; mostly not accepting contributions.

## Harness question (checked 2026-07-30)

No custom harness. They build ON TOP of the existing agent CLIs, same as wiki — but via official SDKs where one exists. `ClaudeAdapter.ts` wraps `@anthropic-ai/claude-agent-sdk` `query()` sessions (the SDK spawns/manages the claude code process itself): typed `SDKMessage` stream, `CanUseTool` permission callbacks, context-usage control requests, interrupt, durable session ids, per-instance HOME isolation so two Claude accounts don't cross-contaminate. Wiki drives the raw CLI (`claude -p --input-format stream-json` over stdin pipes) — that hand-rolled control channel is exactly what detached in the 07-30 orphan incident. Migrating wiki's claude driver to the agent SDK is a candidate follow-up to [[t3code]]-inspired WIKI-219 (event-sourced command log).

