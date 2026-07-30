---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-30
---

Todo:

- [P3] WIKI-182 verdict archaeology — search across all archived reviewer verdicts. "Show every finding about mutation-not-load-bearing." Training data for future reviewer prompts + doctrine mining. Backend: full-text over archived transcripts. Frontend: search page w/ severity + author + date filters
- [P3] WIKI-183 cross-project search — grep across all orch vaults + PRs + commits + verdicts. Wiki + phoebe + tooling + misc in one query. Backend: multi-repo indexer (respect .gitignore); frontend: unified palette
- [P4] WIKI-184 voice steer — Henry says "restart WIKI-165" or "route WIKI-172 R1 to implementer"; app parses via speech-to-text + LLM intent extraction + routes. Overkill but cool. Depends on macOS speech API + safety confirm on destructive ops
- [P3] WIKI-185 auto-plan mode — given Linear-style problem statement, LLM decomposes into subtasks; each renders as workgraph node; orch can edit/reorder before spawning. Backend: planner endpoint. Frontend: plan editor
- [P3] WIKI-186 steer macros — reusable snippets in composer (reviewer prompt, mutation-verify contract, canonical-writer regression, iteration-cap check); one-click apply w/ ticket-name substitution. Cuts recurring prompt drafting
- [P3] WIKI-187 hot.md dedicated editor UI — arc-boundary rewrite surface: split-pane w/ live preview, section templates (Active threads / Recent facts / Watchouts), word-count budget indicator (≤500 target). Currently hand-edited via Read/Edit tools
- [P2] WIKI-177 blast radius view pre-spawn — PARKED 2026-07-30 by overnight orch after 7 review rounds: PR #147 open at 53ecaf7 + round-7 attestation refactor pushed; two structural false-clear blockers remain (blast_radius_cache.py:44 raw-tuple bypass; blast_radius_types.py:300 count-vs-identity fold — dropping an attestation passes all tests). Worktree .codex/worktrees/wiki-177-blast-radius + branch preserved. Resume = fresh implementer on those two items + REVIEW8, or Henry descope/close call
- [P2] WIKI-218 terminal pane integration polish — embedded terminal feels bolted-on (screenshot /tmp/wiki-uploads/ea832ef0826a.png): header shows raw terminal:// UUID instead of a friendly title (cwd/command/user rename), webgl + live-pane chips don't read at a glance, small prompt area floats over a large dead black region (fit/reflow the pty to the pane, kill letterboxing), chrome doesn't match app layout language (padding, focus states, split-pane resize ergonomics, scrollback styling). Layout-first per solid north star. Frontend ticket: cc fable-5 worker + /frontend-design + /make-interfaces-feel-better at spawn
- [P1] WIKI-219 event-sourced supervisor command log — adopt t3code engine pattern: all fleet mutations become typed commands through a single-writer queue; pure decider -> events; append + project + durable request_id receipt in one sqlite txn; provider side effects in reactors consuming intent events. Structurally kills the wedge / mass-archive / split-brain / orphan-control-channel class; generalizes WIKI-163 workgraph idempotency backend-wide. Phase P1: agent-op surface (spawn/steer/archive/replace) with current registry as projection; P2 status/liveness; P3 retire snapshot mode. Coordinate with WIKI-217 (tactical fix may land first; must not fight this design). Spec /tmp/WIKI-219-spec.md + [[t3code]]. Slot after in-flight 190/168/157
- [P2] WIKI-220 rebase-bot durable store polluted by test fixtures — worker/reviewer full-suite runs leak retry-test-* jobs into the LIVE store ~/.wiki/agent-runtime/rebase-bot/state.json (57 of 58 jobs on 2026-07-30 were pytest fixtures w/ dead tmpdir worktrees; their terminal escalations get delivered to the wiki orchestrator as noise, e.g. WIKI-175-IMPL binding-mismatch escalation 15:0xZ). Root cause: rebase_durable._durable_state_path falls back to main.AGENT_RUNTIME_DIR (live) when tests do not override it. Fix: pytest fixture forcing an isolated AGENT_RUNTIME_DIR for every rebase-bot test, assertion/guard that test runs never open the live store, and one-time purge of leaked retry-test jobs (all status=completed, outbox empty, so prune-TTL may clear them first). Related: [[WIKI-219]] event-sourced command log supersedes this store long-term
- [P2] WIKI-222 kill nested scroll regions in stream output — thinking traces, tables, and similar elements render inside inner scrollboxes; they should expand to full height in the page flow. Inner scrolling is only justified for genuinely LARGE outputs (e.g. full file reads); pick a generous line/height threshold and only then clamp + scroll (or clamp + expand affordance). Audit all stream renderers (thinking/reasoning blocks, markdown tables, tool-result panes) for max-height/overflow clamps

Silky-smooth artifact rendering arc (Henry 2026-07-29):

- [P2] WIKI-193 visual-diff artifact mode — before/after image pair w/ opacity slider (drag L↔R for overlay) + pixel-diff toggle (bright overlay of changed regions). Huge for UI regression review — replaces the current back-and-forth of two screenshots
- [P2] WIKI-194 interactive plot upgrade — kind:plot currently static (likely); make it interactive: hover-tooltip, wheel-zoom, drag-to-pan, box-select range, save-as-png. Plotly.js or D3 depending on payload shape
- [P1] WIKI-195 universal fullscreen inspector — cmd+enter opens ANY artifact fullscreen; escape dismisses; arrow keys nav siblings; consistent chrome (title, download, copy source, close). Kills the inconsistent per-kind inspect flows
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
- [P2] WIKI-144 badge sweep: audit + unify status badges across dashboard, session-header, artifact-list — reuse WIKI-143 design tokens; remove redundant variants, consistent color/weight/shape
- [P2] WIKI-149 code-block copy button + diff artifact kind: hover copy on ``` blocks; new artifact renderer syntax-colors +/- with hunk headers; register kind:diff in artifact router
- [P1] WIKI-154 runs management productization: Agents page/cards/banners/actions/session preview/spawn+replace dialogs — Active/History hierarchy, decision-relevant fields only, IDs/tmux/log-paths in Technical details, provider/auth notices state user impact + next action
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
- [P2] WIKI-157 utility-page refinement: activity/graph/health/token usage — title+loading+empty+error+retry everywhere, graph keyboard/noncanvas access, git/CLI terminology secondary, no false-zero token data
- [P3] WIKI-168 daemon-ize wiki backend — open since graph-engineering D2; survive terminal close, launchd or equivalent
- [P1] PHO-14864 land agent-bash-recs-proto on main behind feature flag (owner: phoebe orch)
- [P2] WIKI-221 fleet-card screencast preview collapsible — agent output preview (ScreencastStrip) in each agent card is always visible and tall; hide behind a click-to-expand disclosure, default collapsed, per-ticket localStorage persistence (wiki-collapsed-folders pattern), reuse chevron collapse affordances; skip strip mount/poll while collapsed; working-diff panel (DiffRenderer, codex-stream-renderers.tsx:356) gets the same section-level disclosure (Henry 2026-07-30). Frontend ticket: cc fable-5 worker + /frontend-design + /make-interfaces-feel-better at spawn — cc:WIKI-221

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
