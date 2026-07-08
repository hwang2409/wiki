---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-08
---

Todo:


In Progress:

- [P1] [PHO-13157](https://linear.app/phoebework/issue/PHO-13157): survey delayed-reply capture + chat-history link (folds 13106+13155, 2 customer reports) — cdx:PHO-13157 worker (gpt-5.4 xhigh)
- [P1] [PHO-13138](https://linear.app/phoebework/issue/PHO-13138): Phase 2 admin-owned tables — plan approved (ticket comment = contract, 8-PR series); cdx:PHO-13138 worker on PR1 (schema/roles/deny-tests)
- [PHO-13215](https://linear.app/phoebework/issue/PHO-13215): Core data products T1 (resolver+book+endpoints) — cdx:PHO-13215 worker; T2=PHO-13216 queued; blocked on repeated remote-only BuildBuddy failure of `//services/worker/handlers/phoebe_event_agent:phoebe_event_agent_test__shard_2__bundle` while the exact target passes locally
- [PHO-13218](https://linear.app/phoebework/issue/PHO-13218): tiered tool mounting (hot set + domain packs + routing evals) — cdx:PHO-13218 worker
- [P2] WIKI-16: pane-resize perf — transient CSS-var drag, commit on pointerup (owner: cc:WIKI-16)

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
- [P1] WIKI-19: watchdog revival v1.1 — preserve original tmux session (capture #{session_name} before kill, new-window -t <session>:), resume by explicit session id verified (13215 got fresh session via --last), re-poke message must include ticket id + status-file path for fresh-session context recovery
