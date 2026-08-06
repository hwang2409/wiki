---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-08-06
---

Todo:

- [P3] WIKI-182 verdict archaeology — search across all archived reviewer verdicts. "Show every finding about mutation-not-load-bearing." Training data for future reviewer prompts + doctrine mining. Backend: full-text over archived transcripts. Frontend: search page w/ severity + author + date filters
- [P3] WIKI-183 cross-project search — grep across all orch vaults + PRs + commits + verdicts. Wiki + phoebe + tooling + misc in one query. Backend: multi-repo indexer (respect .gitignore); frontend: unified palette
- [P4] WIKI-184 voice steer — Henry says "restart WIKI-165" or "route WIKI-172 R1 to implementer"; app parses via speech-to-text + LLM intent extraction + routes. Overkill but cool. Depends on macOS speech API + safety confirm on destructive ops
- [P3] WIKI-185 auto-plan mode — given Linear-style problem statement, LLM decomposes into subtasks; each renders as workgraph node; orch can edit/reorder before spawning. Backend: planner endpoint. Frontend: plan editor
- [P3] WIKI-186 steer macros — reusable snippets in composer (reviewer prompt, mutation-verify contract, canonical-writer regression, iteration-cap check); one-click apply w/ ticket-name substitution. Cuts recurring prompt drafting
- [P3] WIKI-187 hot.md dedicated editor UI — arc-boundary rewrite surface: split-pane w/ live preview, section templates (Active threads / Recent facts / Watchouts), word-count budget indicator (≤500 target). Currently hand-edited via Read/Edit tools
- [P2] WIKI-177 blast radius view pre-spawn — PARKED 2026-07-30 by overnight orch after 7 review rounds: PR #147 open at 53ecaf7 + round-7 attestation refactor pushed; two structural false-clear blockers remain (blast_radius_cache.py:44 raw-tuple bypass; blast_radius_types.py:300 count-vs-identity fold — dropping an attestation passes all tests). Worktree .codex/worktrees/wiki-177-blast-radius + branch preserved. Resume = fresh implementer on those two items + REVIEW8, or Henry descope/close call
- backend: raise RLIMIT_NOFILE soft->hard at sidecar startup (GUI launch = 256 soft; wedge #7 defense-in-depth)
- wiki-native: respawn sidecar after signal exits (currently gives up after SIGKILL; needed manual app relaunch in wedge #7)
- check-aliveness: add daemon-mode sweep (registry tmux windows all NO-WINDOW for daemon-managed fleet; use /api/agents control_attached instead)
- [P2] WIKI-228 durable provider-health notice lifecycle — split from WIKI-154 R3 (orch scope call 2026-07-31): headless supervisor must track per-ticket Claude limit state and publish claude_limit_cleared on confirmed resumed-turn recovery (current clear event lives only in the legacy tmux watchdog, skips headless entries — notices persist forever); replace/archive flows must emit ticket-scoped resolution events consumed by account notices; disk-backed notice store needs a durable cursor/replay so provider failures during backend-closed windows survive reconnect (R3 findings 2+4, /tmp/WIKI-154-REVIEW3-verdict.json). Slot after 224/226 land (supervisor.py conflict)
- [P2] WIKI-229 archive transcript identity — split from WIKI-154 R8 (orch cap-decision 2026-07-31, /tmp/WIKI-154-REVIEW8-verdict.json findings 1-3): pre-existing platform bug exposed by per-archive history rows — /session route treats archived_at as non-authoritative (live run and ticket-only _session_paths cache win; older archive returns cached-newest), sidebar target prefers liveWorker over archivedWorker (history click opens live transcript, double selection), loadOlderEvents/getAgentOlderSession/older-session route drop archive identity. Fix: discriminated live-or-archive TranscriptTarget end to end (route resolves exact archive before current-run lookup, 404 stale ids; archived_at through the older-events path). Conflicts with PR #160 until it merges — slot after
- [P2] WIKI-233 remaining 13 pre-existing frontend suite reds enumerated in PR #168 body (found during WIKI-230) — triage: fixture drift vs real regressions, restore full-suite green so worker gates can run unexcluded
- LC-6: integrate practice runner into roadmap app (misc, in progress, owner: misc orch)
- [P1] phoebe: PHO-15149 refactor umbrella to ORGANIZATION.md layout — ticket filed under epic PHO-15148; blocked by PHO-15082 (last parity PR)
- [P2] wiki backend: boot-sweep follow-ups from 2026-08-05 latency fix — skip terminal-state runs in orphan/costs sweeps + prune accumulated run-history store (multi-GB JSONL streaming still O(history) once per boot; related WIKI-227) + incremental/bounded checkpoint for costs state persistence (changed ticks serialize full history off-loop; deferred from WIKI-227 #180 R4 by orch ruling 2026-08-06)
- [P3] wiki repo hygiene: ~95 stale worktrees under .claude/.codex worktrees (detached review checkouts + branches of long-merged tickets, e.g. 40+ wiki-168-review*) — bulk sweep worktrees+branches for merged/closed tickets, GBs of debris
- [P1] WIKI-249 OpenCode 1:1 run-view fidelity — Henry 2026-08-06: agent run UI 'almost exactly one to one' with OpenCode (reference vault/wiki-app/assets/opencode-ui-reference.png + /tmp/opencode-ref source): turn-header agent·model·duration meta rows, '+ Thought: Nms' duration rows, tinted user bars, packed tool runs, click-to-expand hints, zero extra chrome; systematic side-by-side gap audit -> close every gap; START AFTER WIKI-248 merges (same surfaces)

Silky-smooth artifact rendering arc (Henry 2026-07-29):

- [P2] WIKI-196 artifact hover-preview strip — hovering a chat message with N artifacts shows a mini-strip preview (thumbnail row); click any = expand inline; ambient discovery without click-in
- [P2] WIKI-197 drag-drop composer — drag images/files/folders from Finder into composer; auto-uploads as artifact + embeds as `![]()` or `[[]]` reference. Files >5MB warn before send
- [P2] WIKI-198 file-list peek + side-pane — hover on kind:file-list entry shows peek (first 20 lines or thumbnail); click → opens side-pane w/ full viewer instead of external app hop
- [P2] WIKI-199 universal copy/share/download surface — every artifact has consistent header actions (copy raw, copy image/svg, download, share URL if backend exposes). Currently per-kind ad-hoc
- [P1] WIKI-200 artifact rendering perf pass — blur-up placeholders for all image kinds, virtualized list for many-artifact scrolls (>20), thumbnail cache (indexeddb) w/ mtime invalidation, subtle scale-in / fade-out enter/exit animations (150ms cubic-bezier, tuned per make-interfaces-feel-better). Root cause the current jank
- [P3] WIKI-202 image/PDF OCR + searchable — OCR pass on receive (tesseract or macOS Vision framework); text index searchable via vault + palette. Enables grep over screenshot/PDF content

Unknown provider-stream renderer arc (Henry 2026-07-29 — audit of `disposition=unknown` events across last 50 runs; see backend/app/agent_runtime/normalizer.py classifier):

- [P2] PHO-13800 customer_churned_date timing unreliable (19/29 in one entry window; zero churns Jan-Apr) — backfill true dates or document entry-date semantic; related PHO-13735
- [P2] PHO-13763 PARKED by Henry 2026-07-15: PR 11383 drafted at 909ef288ce, gate-passed (CI green, 0 threads, sol REVIEW4 merge-ready), worker archived — resume = un-draft, re-verify freshness vs main, merge
- [P3] WIKI-126: surface rebrand — rename app-facing identity only (app display name, window title, README header); NAME NOT YET CHOSEN by Henry, blocked until he picks. Internal identifiers stay (repo, wiki CLI, WIKI-* tickets, MCP names, vault paths — codename doctrine, Henry 2026-07-15)
- [P2] amd-shadow invalid index: KORGAN owns fix (PR 11416 closed w/ evidence); prod index still INVALID 0-byte — watch deploys
- [P2] PHO-13826/27/28 exe.dev sandbox arc: enable (SSH key secret + smoke) -> ownership fix -> TTL sweeper; PHO-13829 egress design backlog. Surface merged since PHO-13073/#10608, dormant on missing key
- [P3] wiki backend /metrics endpoint (gauge follow-up)
- [P3] pufferclone /metrics endpoint (gauge follow-up)
- Slack admin agent access for Corey Grissom — confirm with Corey his @phoebe.work email + provisioning plan, then either (a) have him sign into app.phoebe.work with @phoebe.work Google Workspace so Kratos provisions the user row + flip admin=true, or (b) one-time hotfix INSERT app.users row + backfill org_slack_user_mappings.user_id for slack_user_id U0AGES89BLZ (2 org rows: Phoebe Home Care + Orchard St. Homecare, both currently user_id=null). Root cause: no app.users row exists at all — his Slack identity is known but unmapped. Full diagnostic + proposed SQL in phoebe session 2026-07-17.
- [P1] PHO-14029 prod dedupe backfill (bun run people:dedupe-backfill --write) + PHO-14033 unique index migration on people(lower(email)) — EOD 2026-07-22
- [P1] WIKI-155 session + dashboard state completeness: explicit zero-event/working/error variants with recovery, dashboard table skeleton, both empty variants contextual, last-good content survives refresh failure
- [P1] WIKI-158 global resilience + lifecycle states: first run, backend down, provider auth, update available, notices — coherent first-run path, backend outage != empty vault, persistent sign-in state, all states announce success recovery
- [P2] WIKI-159 keyboard + dialog accessibility: kanban/dashboard filters/destructive dialog/context menu — keyboard equivalents for drag/double-click, listbox+menu+dialog semantics complete, focus trap+restore, destructive copy describes outcome+recovery
- [P2] WIKI-160 design-token convergence + shared controls: spacing/radii/motion/icons/shadows, settings+status components — one spacing/radius/motion vocabulary, no dup radius aliases, theme-token shadows, weights cap 600, primitives everywhere (rebase after WIKI-144)
- phoebe: fix mock.calls[0] assertion bug in use_scratchpad_index.test.ts round-trip tests (rode into #12058 per Henry merge call)
- [P3] wiki backend: 2 pre-existing auth-dead timing tests fail on hosts with uptime <3600s (monotonic threshold; found by WIKI-166 REVIEW1 full-suite run 07-27) — make uptime-independent or skip-guard

In Progress:

- [P2] WIKI-181 reviewer diversity harness — spawn N reviewers w/ distinct lenses (correctness / security / perf / test-strength) in parallel; synthesize verdicts. Codified adversarial verify — one lens catches what another misses. Wire into next_review (WIKI-171) as opt-in mode — cdx:WIKI-181

- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P2] [PHO-13669](https://linear.app/phoebework/issue/PHO-13669): Core email/account-book gaps — cdx:PHO-13669 (gpt-5.6-terra)
- PHO-13763 per-org scratchpad (worker cdx:PHO-13763, run bd90b506)
- [P2] PHO-13830 live EHR record fetch tool (admin agent) — worker live
- [P2] PHO-13832 shift classification + calendar artifact (admin agent) — worker live
- [P2] PHO-13826 ModalSandboxBackend + PHO-13827 sandbox ownership — workers live (Modal replaces exe.dev for v0; 13828 lifecycle + 13829 egress design queued)
- mitmweb rebuild: scope and build a clearer live proxy-traffic inspector — owner (misc); merged through B6 (tooling PR #10, 2026-07-21); remaining: P1 packaging
- WIKI-135 dashboard: implementation workers only (drop reviewers/one-shots) — owner cdx:WIKI-135 (luna)
- [P2] WIKI-191 audio artifact kind — new `kind: audio` inline w/ waveform preview + scrubber + speed control; transcript overlay if attached. For voice memos, TTS output, transcription evidence
- [P1] PHO-14864 land agent-bash-recs-proto on main behind feature flag (owner: phoebe orch)
- [P1] [PHO-14963](https://linear.app/phoebework/issue/PHO-14963): implement provider-neutral v3 tool discovery and Anthropic delivery — cdx:PHO-14963
- [P1] [PHO-14975](https://linear.app/phoebework/issue/PHO-14975): implement v3 write registry and diff-first flow — cdx:PHO-14975
- [P1] [PHO-14977](https://linear.app/phoebework/issue/PHO-14977): implement v3 read-only helpers and eval scaffolding — cdx:PHO-14977
- PHO-15151 bash rewrite spec — worker spawned, PR to main (owner: phoebe-dev)
- PHO-15110 schema derivation prototype — worker spawned, PR to umbrella, held until #13193
- PHO-15171 v3 local TUI — worker spawned, PR to umbrella (owner: phoebe-dev)
- PHO-0-DEEPWIKI admin deepwiki investigation — research worker, report to /tmp/PHO-0-DEEPWIKI-report.md
- PHO-15206 chronic recommendation test fixes — worker spawned, PR to umbrella
- PHO-14522 orphan cleanup fix — worker spawned, PR to main
- PHO-14037 on-call transfer — investigate-then-fix worker spawned
- PHO-15253 bash rewrite PRs A/B/C — night shift worker
- PHO-15254 retrieve adoption tranche 1 — night shift worker
- [P1] WIKI-248 transcript follow-ups from #183 round-1 verdict — kill ActivityGroup data-model aggregation (one virtual row per event), restore live/interrupted-state + wiki-238 coverage, composer focus + forced-colors treatment, wiki59 Shiki expectation, font-fallback root cause, working-diff collapsed by default (Henry: 'should not be a thing'); WIP seed branch wiki-248-transcript-followups @ aa0305ce
Backlog:

- [PHO-14974](https://linear.app/phoebework/issue/PHO-14974): implement the planned v3 describe catalog after retrieve and write registry interfaces land
- [PHO-14976](https://linear.app/phoebework/issue/PHO-14976): implement the planned V3 parity batches from the 123-row inventory after registry interfaces stabilize
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
