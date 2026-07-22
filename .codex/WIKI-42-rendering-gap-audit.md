# Rendering-Gap Audit — third-party JSONL viewers vs wiki app

Compiled 2026-07-09 for WIKI-42 handoff. Source: `~/me/fun/tmp/codex-viewer` + `~/me/fun/tmp/claude-JSONL-browser`, compared against `backend/app/transcripts.py` + `frontend/src/agent-session-surface.tsx`. Extends the WIKI-41 rendering-gap inventory. When WIKI-42 reaches migration step 5 (WIKI-41 integration), fold these in.

## Already covered

- Monitor tool spawns, task notifications, live task state (`tasks` kind via `task_reminder`), AskUserQuestion cards — all in WIKI-41 scope.
- `turn_aborted` → wiki emits an `interrupt` event (transcripts.py:568). Codex viewer only stores it as unstructured metadata; wiki is actually ahead here.
- Reasoning summary — wiki renders it; Codex viewer surfaces an additional `encrypted_content` flag when present.

## Gaps to close

| # | Event / block | Format | JSONL identifier | Third-party ref |
|---|---|---|---|---|
| 1 | `progress` (agent lifecycle) | Claude | top-level `type: "progress"` | `claude-JSONL-browser/lib/jsonl/parse.ts:157–250` |
| 2 | `system` marker subtypes (richer than wiki's flat `marker`) | Claude | `type: "system"` + `subtype` field | `claude-JSONL-browser/lib/jsonl/parse.ts:161–171` |
| 3 | `tool_reference` block | Claude | message content block `type: "tool_reference"` | `claude-JSONL-browser/lib/jsonl/parse.ts:332–337` |
| 4 | `image` block (standalone, inside message content) | Claude | message content block `type: "image"` | `claude-JSONL-browser/lib/jsonl/parse.ts:340–349` |
| 5 | `custom-title`, `agent-name` metadata | Claude | top-level `type: "custom-title"` / `type: "agent-name"` | `claude-JSONL-browser/lib/jsonl/parse.ts:173–194` |
| 6 | Encrypted-reasoning flag | Codex | `response_item.type: "reasoning"` + `encrypted_content` present | `codex-viewer/src/server/service/codex/parseCodexSession.ts:287–323` |

## Live-JSONL kinds neither viewer renders, but wiki sees on disk

Seen in `~/.codex/sessions/2026/07/09/` + `~/.claude/projects/*/` samples:

- Codex: `task_started` — currently metadata-only in both codex-viewer and wiki. Contract clause "background tasks observable" likely covers it — verify wiki emits something.
- Claude: `mode`, `permission-mode`, `ai-title`, `file-history-snapshot` — top-level rows unhandled by both viewers and wiki. Some are trivial metadata (`ai-title` = display polish); `permission-mode` transitions ARE user-actionable and belong in the observable-events list per contract (approvals/permissions clause).

## Acceptance for WIKI-42

The handoff brief's rendering clause requires "raw → normalized → UI layers with an inspector/count for events that are rendered, summarized, intentionally ignored, or unknown." Whatever policy applies:

- Rendered: 1, 2 (with subtype), 3, 4, `permission-mode`
- Summarized: 5 (`custom-title`, `agent-name`) into a session-header chip
- Intentionally ignored: `file-history-snapshot`, `ai-title` (with a documented reason in the inspector so it doesn't look like a gap)
- Unknown-bucket count: everything else that arrives without a mapping → visible in the inspector so the next audit self-surfaces

Update this file when the audit is re-run against fresh JSONL.

## Pending AskUserQuestion — native supervisor win

Empirical finding 2026-07-10 during WIKI-41 (#37) verification: **Claude Code TUI does not fsync the `AskUserQuestion` `tool_use` block to its session jsonl until the matching `tool_result` is ready.** Both blocks batch into a single append at answer time. Verified by polling `~/.claude/projects/<proj>/<sid>.jsonl` at 2s intervals while a question was pending — file size + mtime unchanged for the full 55s pending window, then jumped by 2 lines the moment the user answered.

Consequence: any wiki-layer reading the on-disk jsonl can NEVER render pending questions, no matter how tight the polling loop. WIKI-41's `_emit_question_events` fires only after both blocks land, so cards appear only in the resolved state with the picked answer already highlighted.

**This is not a WIKI-41 bug and cannot be fixed in the on-disk path.** It is exactly the observability gap the WIKI-42 supervisor path exists to close:

- The supervisor owns the Claude subprocess and consumes its bidirectional stream-json protocol in real time
- `tool_use` blocks arrive on the wire the moment they're emitted by Claude, well before the user answers
- Under WIKI-42, the supervisor's `events()` stream should surface pending AskUserQuestion the moment it lands, letting the frontend render a pending card immediately, then patch it to `answered_option: N` when the response arrives
- Add pending AskUserQuestion + pending approvals (same class of problem) as first-class observable states in the supervisor's event contract, distinct from the existing rendered-when-resolved path

Same class of gap applies to Codex approvals: any provider tool-call that blocks on user input has zero on-disk trace until the answer resolves. Supervisor path fixes both at once.

