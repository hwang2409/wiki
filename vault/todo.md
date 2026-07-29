---
type: reference
view: kanban
tags: [todo]
created: 2026-07-06
updated: 2026-07-27
---

Todo:

- [P3] WIKI-168 daemon-ize wiki backend — open since graph-engineering D2; survive terminal close, launchd or equivalent
- [P1] WIKI-173 orch autopilot — pair next_review (WIKI-171) w/ LLM-parses-verdict + auto-steer + auto-merge on clean. Orchestrator becomes observer, not driver. Backend: verdict-parser module (extract findings via regex + LLM fallback), auto-steer builder, gated auto-merge (requires clean gate + optional Henry ack per ticket). Frontend: autopilot toggle per ticket, live log of autopilot decisions
- [P1] WIKI-174 session replay scrubber — variable-speed replay of any archived agent session from raw.jsonl. Backend: replay endpoint w/ speed control; timeline of tool calls + prompts + responses. Frontend: scrubber UI (like WIKI-169 timeline but for a single session), bookmarks for verdicts/steers/errors. Debug tool for "why did cdx worker do X"
- [P1] WIKI-175 PR conflict auto-rebase bot — detect DIRTY state after upstream merge; spawn dedicated worker that fetches origin/main, merges, resolves mechanical conflicts (imports, ordering, whitespace, lockfiles), pushes. Semantic conflicts escalate to orchestrator w/ diff summary. Cuts 2x rebase cycles seen in 07-29 five-feature arc
- [P2] WIKI-176 live worker screencast strip — miniature terminal preview (last ~20 lines) inline in fleet view + per-ticket cards; refresh 2s from raw.jsonl tail. Skim 12 workers at glance w/o click-in
- [P2] WIKI-177 blast radius view pre-spawn — before spawning ticket X, compute which in-flight branches touch same files (git diff main..branch per active branch); display collision-risk preview in spawn dialog. Prevents rebase pain
- [P2] WIKI-178 cost dashboard — per-worker/ticket/orch/day USD, top spenders, prompt-size distribution, token velocity. Kill runaways before they burn tokens. Backend: cost aggregator over raw.jsonl. Frontend: dashboard page + per-ticket cost strip
- [P2] WIKI-179 vault semantic search — embed vault notes (once, incrementally on write), search by meaning. Grep already exists; add embedding-backed rank. Backend: embedding store + query endpoint. Frontend: search palette upgrade w/ semantic-vs-lexical toggle
- [P2] WIKI-180 auto-context injector on spawn — LLM scans vault + related tickets + recent PRs touching same files + related workgraphs; prepends "context prelude" to kickoff prompt. Cuts prompt-writing time. Backend: prelude builder; hook into spawn_agent. Frontend: preview prelude before spawn
- [P2] WIKI-181 reviewer diversity harness — spawn N reviewers w/ distinct lenses (correctness / security / perf / test-strength) in parallel; synthesize verdicts. Codified adversarial verify — one lens catches what another misses. Wire into next_review (WIKI-171) as opt-in mode
- [P3] WIKI-182 verdict archaeology — search across all archived reviewer verdicts. "Show every finding about mutation-not-load-bearing." Training data for future reviewer prompts + doctrine mining. Backend: full-text over archived transcripts. Frontend: search page w/ severity + author + date filters
- [P3] WIKI-183 cross-project search — grep across all orch vaults + PRs + commits + verdicts. Wiki + phoebe + tooling + misc in one query. Backend: multi-repo indexer (respect .gitignore); frontend: unified palette
- [P4] WIKI-184 voice steer — Henry says "restart WIKI-165" or "route WIKI-172 R1 to implementer"; app parses via speech-to-text + LLM intent extraction + routes. Overkill but cool. Depends on macOS speech API + safety confirm on destructive ops
- [P3] WIKI-185 auto-plan mode — given Linear-style problem statement, LLM decomposes into subtasks; each renders as workgraph node; orch can edit/reorder before spawning. Backend: planner endpoint. Frontend: plan editor
- [P3] WIKI-186 steer macros — reusable snippets in composer (reviewer prompt, mutation-verify contract, canonical-writer regression, iteration-cap check); one-click apply w/ ticket-name substitution. Cuts recurring prompt drafting
- [P3] WIKI-187 hot.md dedicated editor UI — arc-boundary rewrite surface: split-pane w/ live preview, section templates (Active threads / Recent facts / Watchouts), word-count budget indicator (≤500 target). Currently hand-edited via Read/Edit tools

Silky-smooth artifact rendering arc (Henry 2026-07-29):

- [P1] WIKI-188 first-class PDF artifact — new `kind: pdf` renderer w/ inline page nav, zoom/fit-width toggle, thumbnail sidebar, native text selection + copy, keyboard nav (arrows / page-up-down / cmd+f in-doc search). PDF.js under the hood. Extends artifact schema; backend accepts base64 or file-path payload
- [P1] WIKI-189 image artifact polish — blur-up progressive load, lazy-load below fold, click-to-lightbox (fullscreen w/ pinch/scroll zoom + pan), copy-to-clipboard, drag-to-download, EXIF strip on receive, `srcset` for retina. Fixes current jank; makes single-image inline previews feel silky
- [P1] WIKI-190 video/GIF artifact kind — new `kind: video` inline player (mp4/webm/gif). Controls: play/pause/scrubber/speed/mute; poster frame lazy-load; loop-by-default for GIFs. Useful for Playwright recordings, mitmproxy captures, animated diagrams
- [P2] WIKI-191 audio artifact kind — new `kind: audio` inline w/ waveform preview + scrubber + speed control; transcript overlay if attached. For voice memos, TTS output, transcription evidence
- [P2] WIKI-192 multi-image gallery + lightbox — when artifact payload = N images (e.g. R1/R3/R7 screenshots in WIKI-172), render as responsive grid w/ captions; click any → lightbox w/ arrow-key nav + pinch-zoom. Currently a file-list dump
- [P2] WIKI-193 visual-diff artifact mode — before/after image pair w/ opacity slider (drag L↔R for overlay) + pixel-diff toggle (bright overlay of changed regions). Huge for UI regression review — replaces the current back-and-forth of two screenshots
- [P2] WIKI-194 interactive plot upgrade — kind:plot currently static (likely); make it interactive: hover-tooltip, wheel-zoom, drag-to-pan, box-select range, save-as-png. Plotly.js or D3 depending on payload shape
- [P1] WIKI-195 universal fullscreen inspector — cmd+enter opens ANY artifact fullscreen; escape dismisses; arrow keys nav siblings; consistent chrome (title, download, copy source, close). Kills the inconsistent per-kind inspect flows
- [P2] WIKI-196 artifact hover-preview strip — hovering a chat message with N artifacts shows a mini-strip preview (thumbnail row); click any = expand inline; ambient discovery without click-in
- [P2] WIKI-197 drag-drop composer — drag images/files/folders from Finder into composer; auto-uploads as artifact + embeds as `![]()` or `[[]]` reference. Files >5MB warn before send
- [P2] WIKI-198 file-list peek + side-pane — hover on kind:file-list entry shows peek (first 20 lines or thumbnail); click → opens side-pane w/ full viewer instead of external app hop
- [P2] WIKI-199 universal copy/share/download surface — every artifact has consistent header actions (copy raw, copy image/svg, download, share URL if backend exposes). Currently per-kind ad-hoc
- [P1] WIKI-200 artifact rendering perf pass — blur-up placeholders for all image kinds, virtualized list for many-artifact scrolls (>20), thumbnail cache (indexeddb) w/ mtime invalidation, subtle scale-in / fade-out enter/exit animations (150ms cubic-bezier, tuned per make-interfaces-feel-better). Root cause the current jank
- [P2] WIKI-201 markdown inline-image polish — smooth rendering of `![](url)` in agent output: loading placeholder, sized-before-load (aspect-ratio hint or naturalWidth probe), click → lightbox, respect prefers-reduced-motion
- [P3] WIKI-202 image/PDF OCR + searchable — OCR pass on receive (tesseract or macOS Vision framework); text index searchable via vault + palette. Enables grep over screenshot/PDF content
- [P2] PHO-13800 customer_churned_date timing unreliable (19/29 in one entry window; zero churns Jan-Apr) — backfill true dates or document entry-date semantic; related PHO-13735
- [P2] PHO-13763 PARKED by Henry 2026-07-15: PR 11383 drafted at 909ef288ce, gate-passed (CI green, 0 threads, sol REVIEW4 merge-ready), worker archived — resume = un-draft, re-verify freshness vs main, merge
- [P3] WIKI-126: surface rebrand — rename app-facing identity only (app display name, window title, README header); NAME NOT YET CHOSEN by Henry, blocked until he picks. Internal identifiers stay (repo, wiki CLI, WIKI-* tickets, MCP names, vault paths — codename doctrine, Henry 2026-07-15)
- [P2] amd-shadow invalid index: KORGAN owns fix (PR 11416 closed w/ evidence); prod index still INVALID 0-byte — watch deploys
- [P2] PHO-13826/27/28 exe.dev sandbox arc: enable (SSH key secret + smoke) -> ownership fix -> TTL sweeper; PHO-13829 egress design backlog. Surface merged since PHO-13073/#10608, dormant on missing key
- [P3] wiki backend /metrics endpoint (gauge follow-up)
- [P3] pufferclone /metrics endpoint (gauge follow-up)
- MITMWEB-F4: full restyle of mitm-inspector web UI for clarity — adopt theme/layout language of ~/me/fun/wiki frontend; spawn fable worker after I1 integration merges — owner (misc)
- Slack admin agent access for Corey Grissom — confirm with Corey his @phoebe.work email + provisioning plan, then either (a) have him sign into app.phoebe.work with @phoebe.work Google Workspace so Kratos provisions the user row + flip admin=true, or (b) one-time hotfix INSERT app.users row + backfill org_slack_user_mappings.user_id for slack_user_id U0AGES89BLZ (2 org rows: Phoebe Home Care + Orchard St. Homecare, both currently user_id=null). Root cause: no app.users row exists at all — his Slack identity is known but unmapped. Full diagnostic + proposed SQL in phoebe session 2026-07-17.
- [P1] PHO-14029 prod dedupe backfill (bun run people:dedupe-backfill --write) + PHO-14033 unique index migration on people(lower(email)) — EOD 2026-07-22
- [P2] WIKI-144 badge sweep: audit + unify status badges across dashboard, session-header, artifact-list — reuse WIKI-143 design tokens; remove redundant variants, consistent color/weight/shape
- [P2] WIKI-149 code-block copy button + diff artifact kind: hover copy on ``` blocks; new artifact renderer syntax-colors +/- with hunk headers; register kind:diff in artifact router
- [P1] WIKI-152 agent-session chrome: header, provider inspector, action-required card, composer help, footer/status rail — kill 'Provider stream', raw/normalized counts, request IDs, Unknown 0, format/token telemetry, tmux punctuation from default chrome; diagnostics in Run details (depends WIKI-148)
- [P1] WIKI-154 runs management productization: Agents page/cards/banners/actions/session preview/spawn+replace dialogs — Active/History hierarchy, decision-relevant fields only, IDs/tmux/log-paths in Technical details, provider/auth notices state user impact + next action
- [P1] WIKI-155 session + dashboard state completeness: explicit zero-event/working/error variants with recovery, dashboard table skeleton, both empty variants contextual, last-good content survives refresh failure
- [P2] WIKI-157 utility-page refinement: activity/graph/health/token usage — title+loading+empty+error+retry everywhere, graph keyboard/noncanvas access, git/CLI terminology secondary, no false-zero token data
- [P1] WIKI-158 global resilience + lifecycle states: first run, backend down, provider auth, update available, notices — coherent first-run path, backend outage != empty vault, persistent sign-in state, all states announce success recovery
- [P2] WIKI-159 keyboard + dialog accessibility: kanban/dashboard filters/destructive dialog/context menu — keyboard equivalents for drag/double-click, listbox+menu+dialog semantics complete, focus trap+restore, destructive copy describes outcome+recovery
- [P2] WIKI-160 design-token convergence + shared controls: spacing/radii/motion/icons/shadows, settings+status components — one spacing/radius/motion vocabulary, no dup radius aliases, theme-token shadows, weights cap 600, primitives everywhere (rebase after WIKI-144)
- phoebe: fix mock.calls[0] assertion bug in use_scratchpad_index.test.ts round-trip tests (rode into #12058 per Henry merge call)
- [P3] wiki backend: 2 pre-existing auth-dead timing tests fail on hosts with uptime <3600s (monotonic threshold; found by WIKI-166 REVIEW1 full-suite run 07-27) — make uptime-independent or skip-guard

In Progress:


- [PR #10475](https://github.com/phoebe-health/phoebe/pull/10475): subagent recommendation parity iteration (harness #10692) — cdx:PR-10475 worker; overfitting watch
- [P?] [PHO-13274](https://linear.app/phoebework/issue/PHO-13274): post account-health automation as per-owner Phoebe Slack threads with sales-call + product-agent context — cdx:PHO-13274 worker
- [P3] [PHO-13367](https://linear.app/phoebework/issue/PHO-13367): fix Slack admin agent shifts-filled-through-Phoebe miscount (scope to callout, dedupe unique shift) — cdx:PHO-13367 worker
- [P4] [PHO-13368](https://linear.app/phoebework/issue/PHO-13368): admin agent run events monospace font — cc:PHO-13368 worker
- [P2] [PHO-13669](https://linear.app/phoebework/issue/PHO-13669): Core email/account-book gaps — cdx:PHO-13669 (gpt-5.6-terra)
- PHO-13763 per-org scratchpad (worker cdx:PHO-13763, run bd90b506)
- [P2] PHO-13830 live EHR record fetch tool (admin agent) — worker live
- [P2] PHO-13832 shift classification + calendar artifact (admin agent) — worker live
- [P2] PHO-13826 ModalSandboxBackend + PHO-13827 sandbox ownership — workers live (Modal replaces exe.dev for v0; 13828 lifecycle + 13829 egress design queued)
- mitmweb rebuild: scope and build a clearer live proxy-traffic inspector — owner (misc)
- WIKI-135 dashboard: implementation workers only (drop reviewers/one-shots) — owner cdx:WIKI-135 (luna)

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
