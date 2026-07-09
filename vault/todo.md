---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-09
---

Todo:

- [P2] WIKI-35: agent session view surfaces status-file state — state badge + blocker banner in agent-session-surface header (blocked/merge-ready visible without going back to /agents); data already in /api/agents
- [P1] WIKI-40: resolver hardening — for LIVE workers resolve transcripts from ground truth: registry window -> pane PID -> lsof open rollout/transcript handle (exact by construction, immune to stale session ids + resume --last rollout forks); registry session id becomes fallback for dead/idle sessions only. Evidence 07-09: three phoebe workers revived via resume --last forked fresh rollouts, app mis-rendered JSONL until hand-pinned via lsof. Sequenced AFTER WIKI-39 (Henry directive)

In Progress:

- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274): compare_agent_runs trace comparison (audit-validated P1) — cdx:PHO-11274 worker
- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P1] [PHO-13277](https://linear.app/phoebework/issue/PHO-13277): admin agent charts as messages + no point limits
- [P1] [PHO-13278](https://linear.app/phoebework/issue/PHO-13278): truncation policy + artifact UI — cdx:PHO-13278 worker
- [P1] WIKI-38: agents-page Replace protocol — kill + respawn orchestrator/worker with same model, kickoff prompt carries role + replaced session id/transcript for context recovery (owner: cdx:WIKI-38)
- [P1] WIKI-39: worker mapping fixes from 07-09 phoebe churn: watchdog revived DEREGISTERED tickets (PHO-13279/13281 zombies at $HOME, no worktree); revivals skipped 'wiki agent update' so registry window ids went stale; revived-without-session-id workers fall back to worktree discovery — mapping wrong in app UI. Fix: watchdog must (1) skip tickets absent from registry, (2) spawn at registry worktree cwd, (3) run agent update --window --session after every revival, (4) WIKI-34 reset parse
- [P1] WIKI-41: native transcript surfaces — render CC/Codex background monitors (Monitor tool spawns, task notifications, live task state) + Claude AskUserQuestion cards (options, picked answer, custom reply) in session view; includes gap inventory of unrendered event/tool types in both CLI transcript formats (owner: cdx:WIKI-41)

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
- [P1] [PHO-13157](https://linear.app/phoebework/issue/PHO-13157): survey delayed-reply capture + chat-history link (folds 13106+13155, 2 customer reports) — cdx:PHO-13157 worker (gpt-5.4 xhigh)
- [P2] WIKI-27: stabilize view-header height on note<->agent focus flips (40px pane jump, pre-existing — header collapses per focused-pane kind; fix per-window) + drop focusState.kind from pane.tsx getLinks deps (4 redundant /api/links per flip)
- [P1] WIKI-34: watchdog limit parser misses same-day reset format ('try again at 8:01 PM' — no date) -> account stays eligible:true and rotation thrashes into dead accounts; parse bare time as today (tz-aware), pin outgoing account reset on every rotation
- [P1] WIKI-36: backend env overrides for agent-registry/status-dir/msg-queue paths (main.py hardcodes /tmp/agent-registry.json — isolated test backends can't detach from live registry without monkey-patching; root cause of the 07-09 composer-probe breach)
