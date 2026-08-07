---
type: reference
tags: [wiki-app, codex, transcripts]
created: 2026-08-07
updated: 2026-08-07
---

# WIKI-266: Codex provider-neutral transcript parity

**Goal:** Equivalent Codex and Claude agent activity produces the same canonical events and shared transcript rows.

**Dependency:** Start after WIKI-265 merges. Build on its safe legacy-wrapper normalization and detailed thinking summaries.

## Evidence

- A 50-rollout audit found 94.2% semantic decoding for legacy `exec` wrappers at PR #201 SHA `ec27230c`.
- PR #201 reviews found invented dead-branch calls, mutable argument drift, incorrect batch result splitting, and unbounded variable recursion.
- Modern app-server items mostly remain raw provider events or Codex-only highlights.
- Screenshot `acd82e1ac22c.png` shows legacy commands that should match Claude rows, plus adjacent bold thought fragments that remain unpolished.

## Scope

- Normalize legacy `function_call`, `custom_tool_call`, `web_search_call`, `tool_search_call`, `local_shell_call`, and every authoritative result event by `call_id`.
- Normalize modern `commandExecution`, `fileChange`, `mcpToolCall`, `dynamicToolCall`, `collabToolCall`, `webSearch`, `imageView`, approvals, and item lifecycle events.
- Buffer bounded result-before-call events. Treat completion as authoritative. Deduplicate replayed starts, deltas, completions, and raw/completed twins.
- Map one actual semantic call to one provider-neutral event. Preserve command output, status, duration, edits, terminal input, and tool metadata.
- Use one shared registry and the existing Claude-compatible tool, diff, running, failed, and approval components.
- Keep non-artifact content as inspectable raw text or JSON. Only `render_artifact` earns rich rendering.
- Never show Codex wrapper JavaScript. Decode only proven straight-line calls and direct `Promise.all` children. Use bounded generic `exec` fallback for ambiguity.
- Normalize simple and adjacent bold Codex summary fragments without changing their text. Keep the encrypted capability marker.

## Definition of done

- Golden fixtures cover every legacy and modern event/result pair above, including result-first, replay, interruption, failure, declined approval, and compaction.
- Equivalent Claude and Codex fixtures have equal canonical `name`, `input`, `output`, `ok`, `archetype`, `summary`, timing, and edit fields where both providers expose them.
- The audited rollout corpus renders 100% of outer calls, exposes zero wrapper JavaScript, and maps at least 99% of proven-safe actual calls correctly.
- Unsafe wrappers always use generic fallback. Tests prove no dead branch, uncalled function, mutation, alias, template, spread, or malformed source invents a call.
- One item identity produces one visible row across raw/completed twins and resumed replay.
- Session deltas patch existing rows without duplication or order changes.
- Screenshot-equivalent shell, read, plan, and web calls match hypothetical Claude rows in structure, syntax highlighting, state, and expansion behavior.
- Backend fixture tests, frontend integration tests, and a fresh 50-rollout audit pass.

## Non-goals

- Decrypt Codex private reasoning. Codex exposes summaries only.
- Rich-render arbitrary tool output. Explicit `render_artifact` remains the trust boundary.
