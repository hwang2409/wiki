---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-09
---

Todo:

- [P1] WIKI-46: `_session_paths` cache stale on handoff — `backend/app/main.py:758` cache only invalidates on `not found[1].is_file()`, but old kind's jsonl often still on disk after handoff (cc→cdx or vice versa), so cache serves stale path forever. Repro (phoebe-mastermind 07-09): PR-10475 handed off cc→cdx at 17:51; registry.current.kind = cdx + session_id pinned; new rollout at `~/.codex/sessions/.../rollout-...019f48dd-....jsonl`; wiki kept rendering the CC transcript. Manual workaround: mv old jsonl out of ~/.claude/projects/ → is_file()=False → resolver reruns → picks up new kind. Proper fix: hook `_changed_registry_tickets` diff (main.py:1480) — pop `_session_paths[ticket]` for every ticket whose current entry changed. Same fix benefits subagent_session route at line 842. Cross-orch handoff from phoebe-mastermind 07-09; every future handoff hits this.
- [P2] WIKI-35: agent session view surfaces status-file state — state badge + blocker banner in agent-session-surface header (blocked/merge-ready visible without going back to /agents); data already in /api/agents
- [P1] WIKI-40: resolver hardening — for LIVE workers resolve transcripts from ground truth: registry window -> pane PID -> lsof open rollout/transcript handle (exact by construction, immune to stale session ids + resume --last rollout forks); registry session id becomes fallback for dead/idle sessions only. Evidence 07-09: three phoebe workers revived via resume --last forked fresh rollouts, app mis-rendered JSONL until hand-pinned via lsof. Sequenced AFTER WIKI-39 (Henry directive)

In Progress:

- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274): compare_agent_runs trace comparison (audit-validated P1) — cdx:PHO-11274 worker
- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P1] WIKI-38: agents-page Replace protocol — kill + respawn orchestrator/worker with same model, kickoff prompt carries role + replaced session id/transcript for context recovery (owner: cdx:WIKI-38)
- [P1] WIKI-39: worker mapping fixes from 07-09 phoebe churn: watchdog revived DEREGISTERED tickets (PHO-13279/13281 zombies at $HOME, no worktree); revivals skipped 'wiki agent update' so registry window ids went stale; revived-without-session-id workers fall back to worktree discovery — mapping wrong in app UI. Fix: watchdog must (1) skip tickets absent from registry, (2) spawn at registry worktree cwd, (3) run agent update --window --session after every revival, (4) WIKI-34 reset parse
- [P1] WIKI-41: native transcript surfaces — render CC/Codex background monitors (Monitor tool spawns, task notifications, live task state) + Claude AskUserQuestion cards (options, picked answer, custom reply) in session view; includes gap inventory of unrendered event/tool types in both CLI transcript formats (owner: cdx:WIKI-41)
- [P1] WIKI-45: cc-path resolver session_id-first — `find_claude_session` (backend/app/transcripts.py:253) currently only uses slug glob `*{ticket.lower()}*` + kickoff-ticket regex fallback; ignores registry session_id entirely (cdx path uses it at line 137). Repro: phoebe cc:PR-10475 session pinned to `599b561a-7d40-4492-ab82-35a3ae91f733`, rollout at `~/.claude/projects/-Users-henry-me-fun-phoebe--claude-worktrees-pr10475-eval-rerun/599b561a-...jsonl`, slug `pr-10475` never matches dir `pr10475`. /agents shows "no session transcript found". Mirror cdx signature: `find_claude_session(ticket, spawned_at, session_id=None)`. When session_id present → glob `CLAUDE_PROJECTS_DIR/**/<session_id>.jsonl` first, return exact match. Slug/kickoff heuristics remain as fallback for dead sessions where session_id was never pinned. Update caller `find_session` in same file + main.py resolvers to pass session_id through. Cross-orch handoff from phoebe-mastermind 07-09; blocking phoebe UI inspection. Owner: cdx:WIKI-45 (spawned) — cdx:WIKI-45
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker

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
