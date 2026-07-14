---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-14
---

Todo:

- [P2] WIKI-88: register `wiki-artifacts` MCP server on orchestrator runs — WIKI-85 wired it only for worker (`role=implement|plan|review`) startup, so orchestrator runs (`role=orchestrator`) can't call `render_artifact`. Check `backend/app/agent_runtime/{claude,codex}.py` startup to see where MCP servers are attached and lift the role gate. No new behavior; identical MCP-config path. Regression: orchestrator + worker both keep MCP config; existing per-run isolation via `WIKI_RUN_ID` env still routes image bytes into the correct `runs/<run-id>/artifacts/`. Test: spawn an orchestrator, call `render_artifact` from it, verify the artifact renders in the orchestrator's session view.
- [P2] WIKI-48: supervisor RPC input validation — add allowlists on the supervisor's RPC dispatch surface (backend/app/agent_runtime/supervisor.py:1556-1567). Currently `agent_id`, `model`, `effort` accept any string; not shell-injection (args passed separately), but bad enum values cause cryptic downstream errors. Add: TICKET_PATTERN guard on `agent_id`; closed allowlist on `model` (per provider); reuse WIKI-38's `REASONING_EFFORTS` on `effort`. Also validate `orchestrator_id`. Reject at dispatch with clear error rather than letting provider CLI fail.
- [P3] WIKI-49: supervisor default paths hardening — `store.py:144` defaults `WIKI_AGENT_REGISTRY_PATH` to `/tmp/agent-registry.json` (world-writable, pre-stage hijack surface if env not set); same for `WIKI_AGENT_STATUS_DIR` at line 151. Change defaults to `$XDG_RUNTIME_DIR/wiki-agent-runtime/` or `~/.wiki/agent-runtime/`. Env override remains supported. Preserve the /tmp default only via explicit opt-in for backward compat during migration.
- [P3] WIKI-50: supervisor normalizer follow-up — acceptance reviewer for #35 flagged that explicit `image` block dispatch + Codex `encrypted_content` flag may not be surfaced through the supervisor normalizer even though the frontend from WIKI-41 (#37) can render them. Audit `backend/app/agent_runtime/normalizer.py` against a JSONL fixture containing both cases and add explicit emit paths if missing. Low-priority polish; hidden by inspector's `unknown` count if drift.
- [P2] WIKI-35: agent session view surfaces status-file state — state badge + blocker banner in agent-session-surface header (blocked/merge-ready visible without going back to /agents); data already in /api/agents
- [P1] WIKI-40: resolver hardening — for LIVE workers resolve transcripts from ground truth: registry window -> pane PID -> lsof open rollout/transcript handle (exact by construction, immune to stale session ids + resume --last rollout forks); registry session id becomes fallback for dead/idle sessions only. Evidence 07-09: three phoebe workers revived via resume --last forked fresh rollouts, app mis-rendered JSONL until hand-pinned via lsof. Sequenced AFTER WIKI-39 (Henry directive)
- [P2] WIKI-60: LaTeX / math rendering via KaTeX — parse inline $...$ and block $$...$$ in markdown.tsx. Add remark-math + rehype-katex to markdown pipeline (or drop KaTeX API directly if we don't use remark). Bundle KaTeX CSS. Support standard math notation used in agent explanations. Requires WIKI-59 markdown pipeline landed first (avoid concurrent markdown.tsx edits).
- [P1] WIKI-61: diff rendering for Edit/Write/MultiEdit tool_use — currently tool_use inputs display as raw JSON dumps. Parse Edit tool_input.{file_path, old_string, new_string} into a unified diff or side-by-side view with line numbers, per-hunk syntax highlighting (via WIKI-59), expand/collapse for long hunks. Same for Write (show as N-line addition), MultiEdit (multiple hunks), NotebookEdit if we render it. Match Claude Code TUI's diff density. Requires WIKI-59 syntax highlighter landed first for hunk colorizing.
- [P3] WIKI-62: Mermaid diagram rendering — assistant emits fenced ```mermaid``` blocks; render inline via mermaid.js (lazy-loaded). Preserve fenced text on toggle-off for accessibility. Match theme (dark/light). Same markdown.tsx surface as WIKI-59/60 — merge order matters.
- [P2] WIKI-63: file-path autolink — text tokens matching path/to/file.ext or path/to/file.ext:LINE become clickable links inside assistant messages, tool outputs, and event rows. Click opens the vault preview if it's a vault path, or a codebase quick-look pane if it's a repo path. Detection: absolute + repo-relative paths with common extensions (py, ts, tsx, js, jsx, md, json, yaml, sh, rs, go, sql). Guard against false positives inside code fences (only autolink in prose).
- [P2] WIKI-65: Cmd-F in-session search — keyboard shortcut opens a search overlay pinned in the session view. Type → highlight all matches across events (assistant, user, tool, thinking), n/N to next/prev, Esc closes. Scrolls virtualization list to bring match into view. Match count in overlay. Case-insensitive default with case-sensitive toggle. No regex for v0.
- [P3] WIKI-66: event anchor + copy-link on hover — each event row gets a hover-visible anchor icon that copies #event-<id> deep-link URL to clipboard. Session view honors incoming hash on load: scroll to event, briefly highlight. Also enables shareable references in vault notes.
- [P3] WIKI-67: copy button on code blocks + tool outputs — every fenced code block, tool_use input JSON, and tool_result payload gets a small hover-visible copy-to-clipboard button in the top-right. On click: brief 'Copied' feedback, then fade. Uses navigator.clipboard.writeText.
- [P3] WIKI-68: streaming text animation — assistant messages currently appear as batched flip-ins when the delta lands. Instead, render tokens as they stream (per content_block_delta partial_text on Claude side, item/agentMessage/delta on Codex side). Requires backend session endpoint to expose partial-text deltas OR frontend polling of raw.jsonl for the currently-streaming event. Subtle typewriter effect (no cursor blink, just append). Falls back to batch render if delta protocol not available (legacy sessions).
- [P3] WIKI-69: sticky turn header — long transcripts drift; add a sticky header at the top of the session view showing 'Turn N of M · <first-message-preview>' for whichever turn is currently in the viewport. Clicking header scrolls to top of that turn. Turn boundaries derived from consecutive user/assistant message clusters (existing turn model, if any; else user-message boundary).
- [P3] WIKI-70: collapse-long-tool-output — tool_result payloads > N lines (say 20) collapse to first 10 + last 5 lines with a middle 'expand N more lines' affordance. Preserves virtualization height. Copy still copies full text. Applies to Bash outputs, Read contents, grep results, etc.
- [P1] WIKI-101: cc render_artifact results render as 'rejected' — structuredContent shadows sentinel. Repro 2026-07-14 run 96e72308 (wiki orchestrator, cc): server _tool_result returns BOTH content=[sentinel_text] AND structuredContent={artifact_id,ok}; Claude Code records the tool_result content as the structuredContent JSON string, so transcripts.py:591 artifact_from_text finds no sentinel -> _failed_artifact_tool -> UI shows 'render_artifact rejected' (artifact 33b1c159 actually rendered server-side, ok:true). Codex path unaffected (mcp_tool_call_end preserves content blocks). Fix candidates: (1) drop structuredContent from wiki_artifacts.py _tool_result (simplest, universal), and/or (2) parser fallback — pending_artifacts meta already holds tool INPUT; on bare {artifact_id,ok} result, reconstruct the artifact event from input payload + artifact_id. Close WIKI-89 e2e gap: assert the RESULT parses into an artifact session event for BOTH providers, not just that tool_use frames land.

In Progress:


- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P1] WIKI-100: knowledge layer — SQLite index (~/.wiki/knowledge.db) over vault + wiki-managed run history. Files stay truth, DB rebuildable. v1: FTS5 + wikilink graph + `wiki search`/`wiki links` CLI + search_knowledge MCP tool; ingest = mtime/hash delta scan (vault) + archive hook (runs), all async off hot path. Budgets: CLI +0ms, archive hook <1s, query p95 <50ms, rebuild <60s. Spec: docs/superpowers/specs/2026-07-14-knowledge-layer-storage-design.md

Backlog:

- [PHO-11469](https://linear.app/phoebework/issue/PHO-11469): re-triage Slack-thread alert investigation
- [PHO-12425](https://linear.app/phoebework/issue/PHO-12425), 12426: sandboxed outreach start + e2e dogfood
- [PHO-11256](https://linear.app/phoebework/issue/PHO-11256)–11260: texQL series
- [PHO-11269](https://linear.app/phoebework/issue/PHO-11269)–11272: canonical data products series
- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274), 11275: trace comparison + prompt/context diffing
- [PHO-11251](https://linear.app/phoebework/issue/PHO-11251), 11264, 11267: artifact actions, playbook authoring, known-issue memory
- [PHO-12211](https://linear.app/phoebework/issue/PHO-12211): vendor record-and-inject primitive for sandbox probes
- [PHO-11598](https://linear.app/phoebework/issue/PHO-11598): Snowflake QA/feedback + trends tool
- [PHO-12134](https://linear.app/phoebework/issue/PHO-12134): Voice QA inspection support
- [PHO-11727](https://linear.app/phoebework/issue/PHO-11727): subagents as nested tasks in /admin/agent
- [PHO-11539](https://linear.app/phoebework/issue/PHO-11539): admin agent as read-only MCP server
- [PHO-11540](https://linear.app/phoebework/issue/PHO-11540): daily run review summary for tool quality
- [PHO-11651](https://linear.app/phoebework/issue/PHO-11651), 11629: playbook input validation + progress events
- [PHO-11535](https://linear.app/phoebework/issue/PHO-11535): fact/inference evidence tiers for RCA answers
- [PHO-11231](https://linear.app/phoebework/issue/PHO-11231): Phoebe Home Care seed data for visual testing
- [PHO-12982](https://linear.app/phoebework/issue/PHO-12982), 12757: reference tickets (harness doctrine, tool brainstorm)
- [P2] WIKI-18: delta protocol misses pending-tool output on the landing poll (client keeps output:null) — reproduced on main e4528c8 by PR #14 reviewer; check dirty_from rewind math
- [P1] [PHO-13138](https://linear.app/phoebework/issue/PHO-13138): Phase 2 admin-owned tables — plan approved (ticket comment = contract, 8-PR series); cdx:PHO-13138 worker on PR1 (schema/roles/deny-tests)
- [P2] WIKI-27: stabilize view-header height on note<->agent focus flips (40px pane jump, pre-existing — header collapses per focused-pane kind; fix per-window) + drop focusState.kind from pane.tsx getLinks deps (4 redundant /api/links per flip)
- [P1] WIKI-34: watchdog limit parser misses same-day reset format ('try again at 8:01 PM' — no date) -> account stays eligible:true and rotation thrashes into dead accounts; parse bare time as today (tz-aware), pin outgoing account reset on every rotation
- [P1] WIKI-36: backend env overrides for agent-registry/status-dir/msg-queue paths (main.py hardcodes /tmp/agent-registry.json — isolated test backends can't detach from live registry without monkey-patching; root cause of the 07-09 composer-probe breach)
