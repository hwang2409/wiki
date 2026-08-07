---
type: reference
tags: [wiki-app, codex, transcripts]
created: 2026-08-07
updated: 2026-08-07
---

# WIKI-266: Codex provider-neutral transcript parity

**Goal:** Equivalent Codex and Claude agent activity produces the same canonical events and shared transcript rows.

**Dependency:** Start after WIKI-265 merges. Build on its safe legacy-wrapper normalization and detailed thinking summaries.

## Architecture

Treat Codex wrapper JavaScript as a small source language. Compile its safe static subset into Wiki's provider-neutral tool-call IR. Then link lifecycle events and render the shared transcript components.

```text
exec(script)
  -> safe static extractor
  -> ToolGroup + ToolCall children
  -> lifecycle linker
  -> shared transcript renderer
```

This is a deliberately incomplete compiler pass. It is not a JavaScript runtime. Never evaluate wrapper source.

The IR has three relevant forms:

- `ToolGroup`: one outer call with `ordered` or `parallel` mode and zero or more child calls.
- `ToolCall`: a proven tool name, literal input, stable identity, and linked lifecycle data.
- `DynamicToolProgram`: one honest outer `exec` call when safe static extraction fails.

The extractor supports only:

- Direct `await tools.name({ literal: "input" })` calls.
- Static object, array, string, number, and boolean arguments.
- Ordered top-level calls.
- Direct `Promise.all([...])` children as one parallel group.

The extractor rejects:

- Variables whose values require execution.
- Conditions, short-circuit expressions, loops, and arbitrary functions.
- Imports, aliases, computed or dynamic property access, and runtime spreads.
- Templates or expressions that require evaluation.
- Malformed or unsupported JavaScript.

Any rejected or ambiguous source produces one `exec · dynamic tool program` row. Keep the source behind an explicit inspector. Do not show it in the normal transcript.

Never guess child calls. Never assign an outer result to extracted children. The lifecycle linker can attach a result to a child only when the provider supplies a proven child boundary or identity. Otherwise, keep the result on the outer group.

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
- Apply the safe source-to-IR grammar above. Keep wrapper JavaScript behind the fallback row's inspector only.
- Normalize simple bold Codex summary fragments without changing their text. Split a complete sequence such as `**thought one** **thought two**` into separate ordered thinking events. Keep mixed or ambiguous markdown as one event. Preserve the encrypted capability marker on every derived event.

## Definition of done

- Golden fixtures cover every legacy and modern event/result pair above, including result-first, replay, interruption, failure, declined approval, and compaction.
- Equivalent Claude and Codex fixtures have equal canonical `name`, `input`, `output`, `ok`, `archetype`, `summary`, timing, and edit fields where both providers expose them.
- The audited rollout corpus renders 100% of outer calls, exposes no wrapper JavaScript in the normal transcript, and maps at least 99% of proven-safe actual calls correctly.
- Unsafe wrappers always use one `DynamicToolProgram` fallback. Tests prove no dead branch, uncalled function, mutation, alias, template, spread, or malformed source invents a child call.
- Ordered calls produce one ordered group. Direct `Promise.all` calls produce one parallel group.
- Child results link only through proven child boundaries or identities. An aggregate outer result never becomes a guessed child result.
- Complete same-line and newline-separated bold Codex summary sequences render as separate thought rows. Mixed prose and generic Claude thinking do not split.
- One item identity produces one visible row across raw/completed twins and resumed replay.
- Session deltas patch existing rows without duplication or order changes.
- Screenshot-equivalent shell, read, plan, and web calls match hypothetical Claude rows in structure, syntax highlighting, state, and expansion behavior.
- Backend fixture tests, frontend integration tests, and a fresh 50-rollout audit pass.

## Non-goals

- Decrypt Codex private reasoning. Codex exposes summaries only.
- Rich-render arbitrary tool output. Explicit `render_artifact` remains the trust boundary.
