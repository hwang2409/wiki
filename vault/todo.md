---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-15
---

Todo:

- [P2] wiki: compact mermaid artifact preview illegible for large diagrams — styles.css:6684 caps svg at 400px, scale-to-fit squeezes text to ~3px; fix = crop + click-to-inspect or min-scale floor (pan/zoom detail exists). File as WIKI ticket when Linear reauthed
- [P2] WIKI-117: test_wiki_artifacts_server role tests inherit WIKI_AGENT_ROLE from environment — fails on untouched main when run from an orchestrator session (worker tools/list shows fleet ops). Tests must clear/pin WIKI_AGENT_ROLE + WIKI_AGENT_ID. Found during #91 gate
- [P3] WIKI-118: #91 hardening follow-ups from gate review — (M) dedupe artifact events by artifact_id in transcripts parse state (raw function_call pair could double-render); (L) completed-but-unparseable sentinel mislabeled 'rejected'; (L) assert in prod path stripped under -O; (L) artifact_from_text skips _validate_text_payload on write path (worker-forged oversized bodies); (L) missing write-time failed-render normalizer test
- [P2] PHO-13800 customer_churned_date timing unreliable (19/29 in one entry window; zero churns Jan-Apr) — backfill true dates or document entry-date semantic; related PHO-13735
- [P2] PHO-13763 PARKED by Henry 2026-07-15: PR 11383 drafted at 909ef288ce, gate-passed (CI green, 0 threads, sol REVIEW4 merge-ready), worker archived — resume = un-draft, re-verify freshness vs main, merge
- [P3] WIKI-126: surface rebrand — rename app-facing identity only (app display name, window title, README header); NAME NOT YET CHOSEN by Henry, blocked until he picks. Internal identifiers stay (repo, wiki CLI, WIKI-* tickets, MCP names, vault paths — codename doctrine, Henry 2026-07-15)
- [P2] amd-shadow invalid index: KORGAN owns fix (PR 11416 closed w/ evidence); prod index still INVALID 0-byte — watch deploys
- [P2] PHO-13826/27/28 exe.dev sandbox arc: enable (SSH key secret + smoke) -> ownership fix -> TTL sweeper; PHO-13829 egress design backlog. Surface merged since PHO-13073/#10608, dormant on missing key

In Progress:


- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P2] [PHO-13669](https://linear.app/phoebework/issue/PHO-13669): Core email/account-book gaps — cdx:PHO-13669 (gpt-5.6-terra)
- PHO-13763 per-org scratchpad (worker cdx:PHO-13763, run bd90b506)
- [P2] PHO-13830 live EHR record fetch tool (admin agent) — worker live
- [P2] PHO-13832 shift classification + calendar artifact (admin agent) — worker live
- [P2] WIKI-124: accepted svg artifact silently not rendered in session view above size threshold — 24KB svg (id 4907cf6e, 14:48Z 2026-07-15) accepted server-side, never displayed; 800B probe (1acaeef1) rendered fine. Silent accept-then-drop masks failure from sender. Fix: render large svg (scroll/scale) OR visible reject like source-validation errors. Bisect threshold; check overlap with mermaid >20-node compact-preview machinery. Related [[WIKI-116]] — cdx:WIKI-116 worker (bundled w/ 116)
- [P2] WIKI-116: render_artifact accepts syntactically-invalid mermaid silently — worker got success, Henry got 'lexical error on line 79'. Validate mermaid at render time (mermaid.parse in frontend-side check is too late; consider bundled mmdc/headless parse in MCP server) OR feed render errors back into session so agents self-correct. Found via TEST-1 artifact (unquoted [/tmp/... label = trapezoid syntax) — cdx:WIKI-116 worker (bundled w/ 124)
- [P2] PHO-13826 ModalSandboxBackend + PHO-13827 sandbox ownership — workers live (Modal replaces exe.dev for v0; 13828 lifecycle + 13829 egress design queued)
- PUF-5..8: pufferclone v1 — HNSW, compaction, S3 store, non-blocking reads (spec: docs/superpowers/specs/2026-07-15-pufferclone-v1-design.md)
- [P2] WIKI-129: make native-build while Wiki.app running breaks live supervisor — bundle swap clobbers running sidecar's _internal (ENOENT on supervisor channel, respawn blocked by stale supervisor.lock, worker spawn/replace dead until app relaunch; hit 2026-07-15 eve). Fix: build to staging dir + atomic swap on app quit, or preflight check refusing rebuild while app runs

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
