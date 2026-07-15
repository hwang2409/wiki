---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-15
---

Todo:

- [P2] wiki: compact mermaid artifact preview illegible for large diagrams — styles.css:6684 caps svg at 400px, scale-to-fit squeezes text to ~3px; fix = crop + click-to-inspect or min-scale floor (pan/zoom detail exists). File as WIKI ticket when Linear reauthed
- [P2] WIKI-116: render_artifact accepts syntactically-invalid mermaid silently — worker got success, Henry got 'lexical error on line 79'. Validate mermaid at render time (mermaid.parse in frontend-side check is too late; consider bundled mmdc/headless parse in MCP server) OR feed render errors back into session so agents self-correct. Found via TEST-1 artifact (unquoted [/tmp/... label = trapezoid syntax)
- [P2] WIKI-117: test_wiki_artifacts_server role tests inherit WIKI_AGENT_ROLE from environment — fails on untouched main when run from an orchestrator session (worker tools/list shows fleet ops). Tests must clear/pin WIKI_AGENT_ROLE + WIKI_AGENT_ID. Found during #91 gate
- [P3] WIKI-118: #91 hardening follow-ups from gate review — (M) dedupe artifact events by artifact_id in transcripts parse state (raw function_call pair could double-render); (L) completed-but-unparseable sentinel mislabeled 'rejected'; (L) assert in prod path stripped under -O; (L) artifact_from_text skips _validate_text_payload on write path (worker-forged oversized bodies); (L) missing write-time failed-render normalizer test
- [P1] WIKI-121: CodeMirror 6 source editor — replace pane.tsx:305 textarea with CM6 (markdown + fenced-code-block language highlighting, theme sync to CSS vars, preserve draft/save contract). Code-editor arc wave 2 (after 119/120)
- [P2] WIKI-122: file-explorer polish — file-type icons by extension, full-path fuzzy match in Cmd+K switcher, recent-files group. Code-editor arc wave 2

In Progress:


- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P2] [PHO-13669](https://linear.app/phoebework/issue/PHO-13669): Core email/account-book gaps — cdx:PHO-13669 (gpt-5.6-terra)

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
- [P1] [PHO-13138](https://linear.app/phoebework/issue/PHO-13138): Phase 2 admin-owned tables — plan approved (ticket comment = contract, 8-PR series); cdx:PHO-13138 worker on PR1 (schema/roles/deny-tests)
