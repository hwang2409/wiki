---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-08
---

Todo:

- [P1] WIKI-24: window-switch state preservation — composer draft, open subagent/review panel, scroll survive N->J->N window switches (inactive windows unmount today; keep mounted-hidden or externalize per-pane state). Repeated pain: in-progress prompts lost when cross-referencing another session

In Progress:

- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274): compare_agent_runs trace comparison (audit-validated P1) — cdx:PHO-11274 worker
- [PHO-13216](https://linear.app/phoebework/issue/PHO-13216): Core data products T2 (dossier+funnel+MCP mirrors) — cdx:PHO-13216 worker
- [PHO-13227](https://linear.app/phoebework/issue/PHO-13227): subagent approval inheritance (13226 T2) — cdx:PHO-13227 worker
- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [PHO-13251](https://linear.app/phoebework/issue/PHO-13251): cross-org feature-adoption read tool (3x demand-validated) — cdx:PHO-13251 worker

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
