---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-31
---

Todo:

- [P3] WIKI-182 verdict archaeology — search across all archived reviewer verdicts. "Show every finding about mutation-not-load-bearing." Training data for future reviewer prompts + doctrine mining. Backend: full-text over archived transcripts. Frontend: search page w/ severity + author + date filters
- [P3] WIKI-183 cross-project search — grep across all orch vaults + PRs + commits + verdicts. Wiki + phoebe + tooling + misc in one query. Backend: multi-repo indexer (respect .gitignore); frontend: unified palette
- [P4] WIKI-184 voice steer — Henry says "restart WIKI-165" or "route WIKI-172 R1 to implementer"; app parses via speech-to-text + LLM intent extraction + routes. Overkill but cool. Depends on macOS speech API + safety confirm on destructive ops
- [P3] WIKI-185 auto-plan mode — given Linear-style problem statement, LLM decomposes into subtasks; each renders as workgraph node; orch can edit/reorder before spawning. Backend: planner endpoint. Frontend: plan editor
- [P3] WIKI-186 steer macros — reusable snippets in composer (reviewer prompt, mutation-verify contract, canonical-writer regression, iteration-cap check); one-click apply w/ ticket-name substitution. Cuts recurring prompt drafting
- [P3] WIKI-187 hot.md dedicated editor UI — arc-boundary rewrite surface: split-pane w/ live preview, section templates (Active threads / Recent facts / Watchouts), word-count budget indicator (≤500 target). Currently hand-edited via Read/Edit tools
- [P2] WIKI-177 blast radius view pre-spawn — PARKED 2026-07-30 by overnight orch after 7 review rounds: PR #147 open at 53ecaf7 + round-7 attestation refactor pushed; two structural false-clear blockers remain (blast_radius_cache.py:44 raw-tuple bypass; blast_radius_types.py:300 count-vs-identity fold — dropping an attestation passes all tests). Worktree .codex/worktrees/wiki-177-blast-radius + branch preserved. Resume = fresh implementer on those two items + REVIEW8, or Henry descope/close call
- [P2] WIKI-223 media-scrub follow-ups split from WIKI-190 R9 (Henry 2026-07-30): [MEDIUM] mp3.py:36 ID3v2.4 footer-present flag — strip must include the 10-byte 3DI footer or valid footer-tagged MP3s fail 'frame stream broken at offset 0'; [MEDIUM] artifact-block.tsx:290 Copy for video/audio copies internal artifact:// UUID — hide Copy for a/v or route through bounded binary fetch; component tests for both kinds. Verdict archived at /tmp/WIKI-190-REVIEW9-verdict.json
- backend: raise RLIMIT_NOFILE soft->hard at sidecar startup (GUI launch = 256 soft; wedge #7 defense-in-depth)
- wiki-native: respawn sidecar after signal exits (currently gives up after SIGKILL; needed manual app relaunch in wedge #7)
- check-aliveness: add daemon-mode sweep (registry tmux windows all NO-WINDOW for daemon-managed fleet; use /api/agents control_attached instead)
- [P2] WIKI-225 strict WebM (EBML/Matroska) scrub + AAC-in-MP4 scrub + artifact kinds — split from WIKI-190 R16/R23 scope decisions (Henry 2026-07-30): AAC deferred after 3 failed review rounds (R21 unvalidated copy, R22 DSE/FIL bypass, R23 incomplete fix + bit-copy CPU hole) — needs a full raw_data_block syntax parser (one channel element + ID_END, strip DSE/FIL/PCE, byte-aligned copies); implement strict WebM scrubber (bounded EBML parse, element allowlist, VP8/VP9/Opus/Vorbis track types), add webm to VIDEO_MIMES, real mixed a/v fixtures, resource regression like mp4 box-count bound. WIKI-190 ships avc1+AAC MP4 and GIF only; PR #150 body amended to drop WebM claim
- [P1] WIKI-227 backend /api/agents starvation under run-history growth (2026-07-31 07:35Z): HTTP API timing out (curl 000 at 8s, wiki agent status intermittent STATUS-ERROR) while MCP socket path stays fast; sidecar pid at 57% CPU after 15h; 2s sample = main thread dominated by __open/__open_nocancel/stat/__getdirentries64 + psynch GIL waits — costs.refresh() 5s runs-dir sweep scales with accumulated run history and starves the event loop (fd leak fixed in 8055e18 but the scan itself is O(history) every 5s). Also /api/agents payload bloated by full worker histories + composer messages (one archive record = 76KB). Fix: throttle/cache/incremental costs scan or move off the event loop (thread/process + mtime cache), and slim /api/agents default payload (histories behind a flag). Monitor degraded but functional; workers unaffected. No restart performed — app relaunch releases all provider processes
- [P2] WIKI-228 durable provider-health notice lifecycle — split from WIKI-154 R3 (orch scope call 2026-07-31): headless supervisor must track per-ticket Claude limit state and publish claude_limit_cleared on confirmed resumed-turn recovery (current clear event lives only in the legacy tmux watchdog, skips headless entries — notices persist forever); replace/archive flows must emit ticket-scoped resolution events consumed by account notices; disk-backed notice store needs a durable cursor/replay so provider failures during backend-closed windows survive reconnect (R3 findings 2+4, /tmp/WIKI-154-REVIEW3-verdict.json). Slot after 224/226 land (supervisor.py conflict)
- [P2] WIKI-229 archive transcript identity — split from WIKI-154 R8 (orch cap-decision 2026-07-31, /tmp/WIKI-154-REVIEW8-verdict.json findings 1-3): pre-existing platform bug exposed by per-archive history rows — /session route treats archived_at as non-authoritative (live run and ticket-only _session_paths cache win; older archive returns cached-newest), sidebar target prefers liveWorker over archivedWorker (history click opens live transcript, double selection), loadOlderEvents/getAgentOlderSession/older-session route drop archive identity. Fix: discriminated live-or-archive TranscriptTarget end to end (route resolves exact archive before current-run lookup, 404 stale ids; archived_at through the older-events path). Conflicts with PR #160 until it merges — slot after

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
- [P1] WIKI-190 video/GIF artifact kind — new `kind: video` inline player (mp4/webm/gif). Controls: play/pause/scrubber/speed/mute; poster frame lazy-load; loop-by-default for GIFs. Useful for Playwright recordings, mitmproxy captures, animated diagrams
- [P2] WIKI-191 audio artifact kind — new `kind: audio` inline w/ waveform preview + scrubber + speed control; transcript overlay if attached. For voice memos, TTS output, transcription evidence
- [P1] PHO-14864 land agent-bash-recs-proto on main behind feature flag (owner: phoebe orch)
- [P1] WIKI-154 runs management productization: Agents page/cards/banners/actions/session preview/spawn+replace dialogs — Active/History hierarchy, decision-relevant fields only, IDs/tmux/log-paths in Technical details, provider/auth notices state user impact + next action
- [P2] WIKI-194 interactive plot upgrade — kind:plot currently static (likely); make it interactive: hover-tooltip, wheel-zoom, drag-to-pan, box-select range, save-as-png. Plotly.js or D3 depending on payload shape
- [P1] WIKI-219 event-sourced supervisor command log — adopt t3code engine pattern: all fleet mutations become typed commands through a single-writer queue; pure decider -> events; append + project + durable request_id receipt in one sqlite txn; provider side effects in reactors consuming intent events. Structurally kills the wedge / mass-archive / split-brain / orphan-control-channel class; generalizes WIKI-163 workgraph idempotency backend-wide. Phase P1: agent-op surface (spawn/steer/archive/replace) with current registry as projection; P2 status/liveness; P3 retire snapshot mode. Coordinate with WIKI-217 (tactical fix may land first; must not fight this design). Spec /tmp/WIKI-219-spec.md + [[t3code]]. Slot after in-flight 190/168/157. ACCEPTANCE ADDITIONS from WIKI-226 R10 descope (orch 2026-07-31, /tmp/WIKI-226-REVIEW10-verdict.json): (a) pre-start snapshot + txn marker persisted with the run — restart between create() and commit_start() aborts the uncommitted start and restores prior registry/status; (b) run/start request IDs persisted with RunRecord/registry — restart or cache eviction still replays a durable successful start instead of 409
- [P1] [PHO-14972](https://linear.app/phoebework/issue/PHO-14972): v3 workspace service and spill middleware — cdx:PHO-14972
- [P1] [PHO-14975](https://linear.app/phoebework/issue/PHO-14975): plan v3 write registry and diff-first flow — cdx:PHO-14975
- [P1] [PHO-14973](https://linear.app/phoebework/issue/PHO-14973): implement the reviewed v3 retrieve registry plan after PHO-14972 — plan on `henry/phoebe-v3-agent`

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
