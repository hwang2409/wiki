---
type: log
tags: [log, done]
created: 2026-07-06
updated: 2026-08-06
---

# Done

## 2026-08-06

- WIKI-248 merged via #185 (review gate waived by Henry at round 2): per-event spacing model, live-state anchor, data-level caps, exact assertions, working-diff summary
- WIKI-250 merged via #184: Gruvbox dark retuned to gruvbox-dark-hard-contrast.terminal (21 keys decoded + verified twice; derived elevations; selection contrast fixed)
- WIKI-247 merged via #183 (owner call mid-round-2): per-event units, quiet composer, native blocks, font+sidebar fixes; residual review findings split to WIKI-248
- **wiki** — WIKI-247 fable-5 worker hit Anthropic credit wall mid-implementation (resets Aug 10 2am ET); WIP snapshotted at 97cb298, seat replaced with cdx luna + continuation contract
- WIKI-245 merged via #182: OpenCode two-tier transcript (inline rows + left-border blocks, state-as-color, Edit diffs from bounded normalizer payload, harness-wrapper semantics, raw access everywhere) — 7 rounds incl. 3 orchestrator take-over rounds
- WIKI-246 merged via #181: OpenCode chrome language (elevation tokens, left-bar semantics, selection fill, dialog anatomy, motion + hover budgets) — 7 rounds incl. 2 orchestrator take-over rounds
- WIKI-227 merged via #180: incremental costs scan, atomic run publication, dirty-save retry + heartbeat gating, slim /api/agents payload (5 rounds; changed-tick serialization deferred to store-pruning todo)
- WIKI-242 merged via #179: agent-surface a11y pass (focus restore, escape paths, roving menus, 24px targets, 320px/400% reflow) after 4 review rounds
- **wiki** — landed 2026-08-05 uncommitted hotfixes direct to main (3c180a3 orphan-sweep once-per-boot, a0b4f0c agent-prose monospace, c1c86e2 build-script venv python) + vault batch 6970c62; boot-sweep follow-up todo filed

## 2026-08-05

- WIKI-241 landed via #178 (PR #177 closed superseded): action-labelled tool output controls + sticky head merged inside WIKI-244
- WIKI-244 merged (#178): OpenCode-style always-open transcript, block vim cursor, numeric font weight; 10 review rounds, cache-concurrency hardening rode along
- Merged #13238 MCP account-note Slack threads (dash-phoebe's PR, PHO-15150; 4 review rounds: narrow DLQ catch, discriminated responses, operation-UUID idempotency); Henry-authorized
- Merged #13193 v3 shadow-parity harness into umbrella under standing auth (PHO-15082; gate 3 identical/10 explained/1 pending-allowlisted/0 unexplained; ~12 impl rounds, 10 reviews)
- Merged #13519 PHO-15280 no_reply fix (reflection episode removed, prompt-owned reflection); Henry-authorized
- Merged #13501 Slack approval caller-name fix (PHO-15269, RAH Miami report); 3 review rounds; Henry-authorized
- PHO-15082 #13193 citation contract polished at `31dd8b8761ff117d965d5abefc76bb78486ea4ba`; hosted parity green; PR ready for review
- Merged #13507 umbrella frontend fixes (timer test sync + v3 flag toggles) under standing auth; unblocks #13193 CI
- Merged #13384 revenue-account migration follow-up (PHO-14522) after verifying #13383 code deployed to prod; Henry-authorized
- v3-main integration constructed: #13408 (read+bash+control, fully green, review-verified) + #13417 (write surface byte-identical, held); gates = parity hardening + Henry auth + write design pass
- PHO-15254 retrieve adoption tranche 1 gated (#13414, 4 rounds; typed read libraries, v2 tools behavior-locked with falsifier contract tests; v3 adapter adoption deferred as deliverable 2)

## 2026-08-04

- PHO-14522 deploy 1 merged to main (#13383; RingCentral + notification-destination org-deletion 500s fixed; #13384 migration held for post-deploy)
- PHO-15222 surface-aware admin action audit merged to main (#13377, 2 rounds; Slack mutations unblocked, honest no-backfill migration)
- PHO-15207 merged #13360
- PHO-15207 schema-derivation rollout phase 1 merged to umbrella (#13360, first round clean; caregivers/clients/scheduling rows drift-impossible, 8 skips classified)
- PHO-15178 PAT last_used_at throttle merged to main (#13334, Henry's implementation + fleet-added tests; closes the production row-lock RCA)
- PHO-15206 chronic recommendation test fixes merged to umbrella (#13359, first round clean; write suite now candidate 15/15, allowed-failures asterisk dead)
- PHO-15178 tests added to Henry's #13334 (two-session SKIP LOCKED proof, window contract, revocation-after-marker)
- PHO-15175-FIX403 PR #13353 closed at Henry's request (#13350 grant suffices); hardening+regression-test recipe preserved in closed PR + RCA
- PHO-15110 schema-derivation prototype merged to umbrella (#13294, first-round clean; drift-impossible retrieve rows on care-coordinator extension)
- PHO-15178 PAT-auth fix PR #13331 closed at Henry's request; ticket stays in backlog (defect documented in RCA)
- PHO-15174 deepwiki observability merged to main (#13302, 3 rounds + cardinality fix; parent spans on all entry points, fail-open telemetry, snapshot staleness signal)
- PHO-15171-FIX TUI log interleaving merged to umbrella (#13330; additive initialize_tui_observability entry point, rotating file sink, console WARNING+)
- PHO-15081 v3 w3 writes merged to umbrella (#13260, 11 rounds; durable child intents, worker lease renewal, parent-completion-gated-on-child, locked-test discipline held)
- PHO-15175 deepwiki performance merged to main (#13304, 5 rounds; async post-cutover indexing behind authorize, version-scoped pointer lanes, deterministic retained refresh, parallel shard reads + single boto3 client)
- PHO-15156 worktree_dev hosting fixes merged to main (#13293, 5 rounds; no-auto-stamp drift detection, SSO typed fallback, PEM quoting, tmux fix)
- PHO-15171 v3 local TUI merged to umbrella (#13300, 3 rounds; DSN locality guard, mode listeners, diff-before-approve verified)
- PHO-15087 provisional decision recorded: compute-delegate mini-harness = entire v3 subagent surface (Henry 2026-08-04)
- PHO-15151 bash rewrite spec merged to main (#13283, 3 review rounds)
- PHO-15138 scratchpad memory merged to main (#13233, admin-merge past security-bot diff cap per Henry)
- PHO-15110 schema-derivation prototype: both gates passed first round (PR #13294, MERGE-READY verdict); held for #13193 then orch merges
- PHO-15148 triage: canceled 9 obsolete pre-restart tickets (14972-14976, 15061-15064) as superseded by the merged parity arc
- PHO-15115 #13259 merged into umbrella: v3 error/retry parity — single retry budget via wrapper-safe one-shot exec, run_bash transient/terminal split, production-persisted sad-path evals (4 review rounds)
- PHO-15084-TYFIX #13261 merged: umbrella tip ty-clean again (narrowing-assert fix; orchestrator override of over-strict review spec, documented on PR)
- PHO-15084 #13257 merged into umbrella: v3 control surface — ask_user_question + debug_request_approval as one control skill, real lifecycle tests, idempotent untrusted-wrapped answers, debug probe non-prod only (3 review rounds)
- PHO-15114 #13258 merged into umbrella: v3 model policy port (customer reply stays opus/medium, 5 bounded-mechanical contracts at haiku tier, no hardcoded models; 2 review rounds)
- PHO-15149 #13255 merged into umbrella: ORGANIZATION.md refactor — core/skills/sidebar layout, census 47/47 files (-12 vs before), limits test clean, layering enforced in Bazel, behavior preservation AST-proven (3 review rounds)
- PHO-15065 #13256 merged into umbrella: EXPERIMENTAL in-sandbox subagent runtime (real model loop via broker, 6 fail-closed invariants, lane green; 1 review round clean)

## 2026-08-03

- PHO-15085 #13251 merged into umbrella: v3 hardening audit — invariant-5 closed (file-inspection failure paths + fallback wrapper), TTL sweep bounded + @process, retrieve/bash observability (3 review rounds)
- PHO-15147 #13249 merged into umbrella: agent_sandbox post-merge findings + all 8 Devin threads resolved incl SEC triage (2 review rounds)
- PHO-15086 #13250 merged into umbrella: v3 rollout routing plan + per-surface flag skeleton (1 review round, clean)
- PHO-15112 #13196 merged into umbrella: containment lane green end-to-end (7-8 production bugs fixed: streams, flush, result synthesis, fence, overflow terminal, crash capture); follow-ups in PHO-15147
- PHO-15080 #13191 merged into umbrella: v3 write entities + receipt replay ordering (6 review rounds)
- PHO-15079 #13192 merged into umbrella: v3 retrieve domain parity (7 review rounds)
- PHO-15083 #13190 merged into umbrella: v3 golden case eval runner (8 review rounds)
- PHO-15141 #13237 merged: flaky wall-clock deadline test fixed (behavior assertions, total-deadline regression detection restored)
- PHO-15125 #13198 merged: revert of #12876 (bash recs + agent_sandbox/spill libs off main; rewrite pending per ORGANIZATION.md step 1)
- WIKI-239 coalesced provider diagnostics merged (#176 as 48eabd1) — group by source+kind+code, severity fold keeps highest, raw retention, advisory clamp notices, transition-only auto-open respecting user close; 3 review rounds (2 lens R1, 2 lens R2, fix-verify R3); full frontend suite 264/264 (WIKI-233's 13 reds no longer reproduce)
- WIKI-238 semantic model activity + readable transcript hierarchy merged (#174 as 09b8b83) — semantic activity summary from fixed archetype allowlist, prominent state label with live-priority + explicit interrupted/dead terminals, rectangular expanded timeline in completion order, per-role contrast 4.5:1 across 13 themes × 5 states. 4 review rounds (6/8/1/0 findings)
- WIKI-243 supervisor startup recovery memory streaming merged (#175 as 16a3b2a) — _reconcile_existing_runs + _normalize_orphan_raw_events now stream via _iter_json_lines instead of materializing full raw/normalized lists; WIKI-232 REVIEW9 F2 middle-gap + REVIEW12 H1 legacy-boundary invariants preserved

## 2026-08-02

- LC-6 round 2 misc/lc: fold cross-tab storage listener + runner.js path/HEAD nits (LC-6-REVIEW1 findings)
- LC-6 misc/lc: one-server integration merged (roadmap + practice unified, deep-links + shared progress)
- bumped supervisor slow-lane timeout 30s->120s (client.py); rebuilt + restarted Wiki.app. run/replace cold-spawn of orphaned providers post-restart genuinely exceeds 30s under fleet load.
- restarted Wiki.app + rebuilt from source with client.py timeout fix. gotcha: build/swap scripts (native_backend_fingerprint.py, native_swap_transaction.py) invoke bare python3 which is homebrew (no PIL); must prepend .venv/bin to PATH or scripts fail post-PyInstaller. also: hold_app_lock blocks swap while Wiki.app is live — kill Tauri first.
- supervisor RPC: bumped fast-read/ping timeout 1s->3s and ensure_running default 5s->15s (backend/app/agent_runtime/client.py) — kills the '1s ping timed out' 503 spam under load
- **wiki** — WIKI-232 command-log recovery hardening merged after 26 review rounds ([#167](https://github.com/hwang2409/wiki/pull/167), squash `bc31896a`)
- **wiki** — WIKI-240 run-header hierarchy merged ([#173](https://github.com/hwang2409/wiki/pull/173), squash `8a261341`)
- **wiki** — WIKI-225 strict VP8 keyframe-only WebM scrub merged ([#172](https://github.com/hwang2409/wiki/pull/172), squash `c8404590`)
- **wiki** — WIKI-235 runs UI declutter merged ([#171](https://github.com/hwang2409/wiki/pull/171), squash `262795d0`)
- **wiki** — WIKI-237 agent prompt dock merged ([#170](https://github.com/hwang2409/wiki/pull/170), squash `a672bc95`)
- **wiki** — WIKI-236 UX diagnosis audited; WIKI-237–242 implementation split prepared
- **misc** — LC-2 concept visuals merged after 3 review rounds (cdx luna implement + cdx sol review) — 32 canvas concept visuals with DPR-correct rendering, idempotent cleanup, dedupe-before-construct card opens, theme repaint, aria-valuetext sliders, narrow-width layouts; LC arc (LC-1..LC-5) complete
- **misc** — LC-5 local leetcode practice runner merged after 3 review rounds (cdx luna implement + cc opus review after cdx sol cyberPolicy block) — 150-problem bank, python+js graders, per-slug outer/all unordered canonicalization, per-test SIGALRM timeouts, clone identity checks, submit/accepted tracking; verdicts in /tmp/LC-5-REVIEW*-verdict.json
- wiki — WIKI-234 opencode-inspired terminal restyle merged as https://github.com/hwang2409/wiki/pull/169 (2 rounds, squash 0db025d)
- lc roadmap app: LC-3 white default theme + 17-theme lab (themes.html, localStorage apply)
- lc roadmap app: LC-4 cojudge import — 150/150 problems with statements, solutions, tests in app/problems/
- lc roadmap app: LC-1 card dismissal rules (root-sweep + drag-to-pin) and article pane rail, fable-5 worker, orchestrator-gated
- lc roadmap app: native rewritten wikipedia articles (32) with in-app article cards; 4 parallel writer agents
- WIKI-230/231 merged as https://github.com/hwang2409/wiki/pull/168 (native-transcript suite fix + mp4 timing alias, 1 round, squash edddbdd)
- lc roadmap app: built NeetCode-150 reader-style web app in ~/me/fun/misc/lc/app (concept cards, progress tracking)
- WIKI-223 merged as https://github.com/hwang2409/wiki/pull/166 (ID3v2.4 footer strip + a/v copy action, 2 rounds, squash d0c3045)
- WIKI-219 merged as https://github.com/hwang2409/wiki/pull/165 (event-sourced supervisor command log, 11 rounds + merge-integrity pass, squash b972a34; follow-ups WIKI-232)
- WIKI-154 merged as https://github.com/hwang2409/wiki/pull/160 (runs-mgmt account-health notices, 27 rounds, squash a7354b5)
- WIKI-190/191 merged as https://github.com/hwang2409/wiki/pull/150 (media artifact kinds, 46 rounds, squash 1236725)

## 2026-08-01

- PHO-14969 merged: PR #13031 migrate direct admin tools into registry (squash, 4 review rounds)
- v3 harness COMPLETE on henry/phoebe-v3-agent at 05d616e98d: all seven steps folded (describe, parity, tool-search, helpers, workspace, retrieve, write); bazel 74/74 + ty clean
- PHO-14975 v3 write step clean after 10 review rounds at f5f0f31183; final fold queued
- v3 harness: retrieve step (PHO-14973) folded into henry/phoebe-v3-agent at da184279d4 under the fenced-lease reconciliation; bazel 63/63 + ty clean; only write step remains
- v3 harness: workspace step (PHO-14972) folded into henry/phoebe-v3-agent at 964d1027f3; bazel 59/59 + ty clean
- v3 harness: retrieve step (PHO-14973) folded into henry/phoebe-v3-agent at da184279d456; bazel v3 12/12, sandbox 15/15, spill 2/2, llm 33/33, worker 1/1 + ty clean

## 2026-07-31

- **phoebe** — PHO-14989 controlled audit execution restored for account-health drift via [#13059](https://github.com/phoebe-health/phoebe/pull/13059) (squash 97904d9a73)
- **wiki** — WIKI-194 interactive plot controls ([#163](https://github.com/hwang2409/wiki/pull/163) merged 34fb7fc)
- **wiki** — visual-diff artifact mode with image comparison controls ([#164](https://github.com/hwang2409/wiki/pull/164) merged 367a0f7)
- wiki — WIKI-226 spawn-verification hardening merged via [#161](https://github.com/hwang2409/wiki/pull/161) (squash a65a469, 11 rounds — load-tolerant provider verification, scoped atomic rollback w/ status-file ownership, backend-URL propagation across all callers, canonical reviewer identity w/ legacy-key compat, server-side implicit ids w/ bounded dedupe; restart-durability descoped to WIKI-219)
- **phoebe** — staging+prod app-env-vars backfilled with agent v3 Modal/sandbox keys (MODAL_ENVIRONMENT, MODAL_TOKEN_ID/SECRET, AGENT_SANDBOX_WORKSPACE_TOKEN_SECRET); prod already had Modal tokens, staging had none; workspace-token secrets freshly generated per env; picked up on next regular deploy
- **phoebe** — PR #13002 merged (6d890b51d7) — PHO-14953
- **phoebe** — PR #13011 merged (ba1be0f199) — staging deploy fix: agent v3 sandbox/Modal creds demoted from boot-required to use-time fail-fast; unblocks deploys broken since 05:04Z; ops follow-up: add secrets before agent_v3_enabled flips
- wiki — WIKI-224 graph-health false-alarm fixes merged via [#159](https://github.com/hwang2409/wiki/pull/159) (squash 8e3da9d, 6 rounds — spawn-edge grace, per-worker stall evaluation w/ meaningful-activity filtering, stale-only episode identity, incremental bounded event cursors off the O(history) scan, recovery-reset episodes)
- **website** — merged PR #1 — full ma5a-style redesign (tiny type, whitespace, dotted rules, pixel snowboarder mascot, AA contrast) + music/spotify page commit; 3 review rounds to clean pass
- wiki — WIKI-222 nested-scroll-region kill merged via [#162](https://github.com/hwang2409/wiki/pull/162) (squash 3e8015d, 2 rounds — shared height threshold, ResizeObserver clamp for wrapped single-line payloads, full-height stream flow)
- **phoebe** — PHO-14952 v3 file inspection tools merged via [#13000](https://github.com/phoebe-health/phoebe/pull/13000) (squash beed18398b, 3 rounds — budget-aware streaming reader, row-framing fix; v1 expansion complete)
- wiki — WIKI-168 daemon-ize backend merged via [#153](https://github.com/hwang2409/wiki/pull/153) (squash 295ba9b, 42 review rounds — launchd daemon w/ authenticated secret handshake, handover finalization state matrix, unified install/runtime codesign trust gate; daemon activation on a real install needs Henry's developer signing identity, ad-hoc builds fall back to sidecar)
- **phoebe** — PHO-14951 v3 get_shifts tool merged via [#13001](https://github.com/phoebe-health/phoebe/pull/13001) (squash c01e719bd3, 3 rounds — canonical progression-state semantics, v1 expansion tool 1 of 2)
- **phoebe** — PHO-14880 production voice-call analysis merged via [#12902](https://github.com/phoebe-health/phoebe/pull/12902) (squash f36321e780, 6 rounds incl. omitted-#12986 integrity catch; survived 4 BuildBuddy disk flakes) — admin-automation arc COMPLETE
- **phoebe** — PHO-14864 bash-recs tooling under v3 agent merged via [#12876](https://github.com/phoebe-health/phoebe/pull/12876) (squash 96f894b971, 9 rounds incl. v3 retarget + facade-port catch + merge-integrity round; behind agent_v3_enabled)
- **phoebe** — PHO-14942 EHR drift alerts merged via [#12986](https://github.com/phoebe-health/phoebe/pull/12986) (squash 850d832384, 2 rounds, deterministic daily snapshots)
- **phoebe** — PHO-14844 call-analysis auto-apply tools merged via [#12854](https://github.com/phoebe-health/phoebe/pull/12854) (squash a97b056039, 8 rounds incl. mid-loop no-approval contract change)

## 2026-07-30

- **phoebe** — PHO-14881 per-org account-health drift watch merged via [#12898](https://github.com/phoebe-health/phoebe/pull/12898) (squash d8706c9379, 6 review rounds converging 4-3-3-1-clean; H1 read-only enforced, EHR metric deferred pending historical source)
- **phoebe** — PHO-14912 approval-notification caller name + chat link merged via [#12955](https://github.com/phoebe-health/phoebe/pull/12955) (squash 3e8d4f4861, 2 review rounds, customer: Nick Breus RAH Miami)
- **phoebe** — PR #12926 eval-harness provenance truthfulness merged (squash 627c80437f, 5 review rounds + implementer swap; caller-evidence circularity broken, PHO-14286 arc fully closed)
- **phoebe** — PHO-14899 outreach approval-card preview merged via [#12933](https://github.com/phoebe-health/phoebe/pull/12933) (squash 41fcbb1769, 3 review rounds, customer: jsall)
- **phoebe** — PHO-14595 worker archived at both-gates-clean (#12574 parked open @4fb9bcbb, 8 review rounds; Henry rethinking ticket direction, worktree .claude/worktrees/pho-14595 kept)
- **phoebe** — PR #12892 admin-agent analysis-log read access merged (squash 8ee076041b, Devin PR taken over, 3 review rounds)
- **phoebe** — PHO-14286 confirmed merged via [#12142](https://github.com/phoebe-health/phoebe/pull/12142) (merged 07-24, gate was stale); post-merge truthfulness commits recovered into follow-up [#12926](https://github.com/phoebe-health/phoebe/pull/12926)
- **phoebe** — PHO-14881-HOTFIX merged via [#12910](https://github.com/phoebe-health/phoebe/pull/12910)
- **wiki** — WIKI-218 terminal pane integration polish ([#158](https://github.com/hwang2409/wiki/pull/158) merged 8b5557f83cbba0ba15d50f17d6d8988fac5b8e5c)
- **wiki** — WIKI-220 rebase-bot durable store isolation ([#155](https://github.com/hwang2409/wiki/pull/155) merged b9bcec6bdd78463673e1931232d8d547fb2da471)
- **wiki** — WIKI-195 universal fullscreen inspector ([#157](https://github.com/hwang2409/wiki/pull/157) merged 956ef9f4238354513f5a2b482693d502048f216b)
- **wiki** — WIKI-157 utility-page refinement merged (PR #154, 6 review rounds — false-zero token contract, health/activity/graph states)
- WIKI-221 fleet-card screencast preview + working diff behind disclosures, default collapsed (PR #156, merged)
- phoebe — PHO-14845 admin Slack channel + alert tools merged (#12856 squash @ccc75e4fe5, 3 review rounds + conflict-resolution merge)
- phoebe — PHO-14851 Phoebe Home Care admin_agent_tool_search enable merged (#12855 squash @01d77efe58, 3 review rounds)
- phoebe — PHO-14843 admin scratchpad hardening merged (#12857 squash @1dd94824d2, 2 review rounds)
- **wiki-app** — WIKI-152 agent-session chrome merged — PR #152, 3 review rounds; default chrome shows decision-relevant state only, all diagnostics consolidated in Run details disclosure, absence tests guard against noise creep, composer a11y fixed, screenshots on PR (https://github.com/hwang2409/wiki/pull/152)
- **wiki-app** — WIKI-181 reviewer diversity harness merged — PR #151, 4 review rounds; opt-in N-lens parallel review on next_review w/ deterministic synthesis (exact-set + exact-SHA strictness, absence=dirty), canonical-handler e2e both paths, lens workers archived correctly (https://github.com/hwang2409/wiki/pull/151)
- **wiki-app** — WIKI-176 live worker screencast strip merged — PR #149, 3 review rounds; bounded EOF-window tails via pathwalk, single batched poller w/ ETag/304, 20-row strips on fleet + ticket cards, ANSI/control stripping (https://github.com/hwang2409/wiki/pull/149)
- **wiki-app** — WIKI-180 auto-context injector merged — PR #148, 3 review rounds; deterministic opt-in spawn prelude (vault+PRs+workgraph), fence-proof envelope, strict read-only retrieval, explicit truncation counts (https://github.com/hwang2409/wiki/pull/148)
- **wiki-app** — WIKI-201 merged via PR #144 (image cluster: polish + gallery + inline images, 6 review rounds; EXIF strip w/ bomb caps, shared lightbox w/ focus trap, dims-before-render CLS kill, blur-up fades, delimiter-safe resolvers both sides)
- **wiki-app** — WIKI-192 merged via PR #144 (image cluster: polish + gallery + inline images, 6 review rounds; EXIF strip w/ bomb caps, shared lightbox w/ focus trap, dims-before-render CLS kill, blur-up fades, delimiter-safe resolvers both sides)
- **wiki-app** — WIKI-189 merged via PR #144 (image cluster: polish + gallery + inline images, 6 review rounds; EXIF strip w/ bomb caps, shared lightbox w/ focus trap, dims-before-render CLS kill, blur-up fades, delimiter-safe resolvers both sides)
- **wiki-app** — WIKI-174 session replay scrubber merged — PR #143, 5 review rounds; bounded fd-first raw.jsonl reads via pathwalk, HMAC-signed resumable cursors, windowed frontend w/ rewind eviction, real-Codex bookmark fixtures (https://github.com/hwang2409/wiki/pull/143)
- **wiki-app** — WIKI-175 PR conflict auto-rebase bot merged — PR #137, 14 review rounds (plateau -> Henry-authority scope cut -> implementer swap luna->opus -> durable-store hardening); resolver scoped to lockfile-regen + line-endings only, everything else escalates w/ diff summary; flock+replace-not-merge durable jobs, exactly-once delivery, atomic prune (https://github.com/hwang2409/wiki/pull/137)
- **wiki-app** — WIKI-179 vault semantic search merged — PR #146, 4 review rounds; embedding index w/ strict privacy invariant (uploads only via explicit wiki index rebuild, verified zero provider calls from all search paths incl. background drain), incremental hash-keyed re-embedding, lexical fallback w/ indexing state (https://github.com/hwang2409/wiki/pull/146)
- **wiki-app** — WIKI-178 cost dashboard merged — PR #145, 5 review rounds; per-worker/ticket/orch/day USD w/ verified provider rates incl. 5-min/1-hour cache-write tiers, cached snapshot endpoint, descriptor-pinned scans, truncation-safe cursors (https://github.com/hwang2409/wiki/pull/145)
- **wiki-app** — WIKI-216 unknown-kind telemetry job merged — PR #142, 5 review rounds; weekly incremental sweep w/ atomic archive publication, crash-safe cursors, durable CLI retry+dedupe, auto-files vault todos for novel unknown kinds (https://github.com/hwang2409/wiki/pull/142)
- **wiki-app** — WIKI-173 orch autopilot merged — PR #140, 8 review rounds; verdict parser w/ alias normalization + SHA-bound merge authority, canonical-graph persistence before action, per-repo merge policy, notifier test isolation w/ live-send trap, strict no-check gate w/ rollup validation (https://github.com/hwang2409/wiki/pull/140)
- **wiki-app** — WIKI-188 first-class PDF artifact merged — PR #141, 5 review rounds; PDF.js renderer w/ per-component dir_fd+O_NOFOLLOW path walk, bounded streaming text extraction on search AND render paths, windowed thumbnails, race-real TOCTOU test (https://github.com/hwang2409/wiki/pull/141)
- **wiki-app** — WIKI-215 folded into PR #138 (Claude provider stream events batch, merged)
- **wiki-app** — WIKI-214 folded into PR #138 (Claude provider stream events batch, merged)
- **wiki-app** — WIKI-213 folded into PR #138 (Claude provider stream events batch, merged)
- **wiki-app** — WIKI-212 folded into PR #138 (Claude provider stream events batch, merged)
- **wiki-app** — WIKI-211 folded into PR #138 (Claude provider stream events batch, merged)
- **wiki-app** — WIKI-210 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-209 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-208 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-207 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-206 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-205 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-204 folded into PR #139 (WIKI-203 Codex stream renderer batch, merged)
- **wiki-app** — WIKI-203 Codex turn/diff renderer merged — PR #139, 9 review rounds, live rolling diff artifact w/ pinned current-turn diff, bounded memory/bytes/files/hunk-headers, snapshot store split from run.json (https://github.com/hwang2409/wiki/pull/139)
- **BASH-BRANCH-MERGE-MAIN** — agent-bash-recs-proto reconciled with main and pushed (0a1f5b8f93); worker archived
- **wiki** — WIKI-217 supervisor fingerprint-swap wedge fixed (dev-client swap guard + early lock release + socket inode guard), merged 5017f26

## 2026-07-29

- **phoebe** — PHO-13647 account-health PostHog batch tool merged (#12623, 3 review rounds + conflict rebase re-verify)
- WIKI-211 merged 1ed52f1 — Claude provider-stream renderers batch (thinking_tokens SUMMARIZED, system:init collapsible, task_notification+task_updated patch cards, api_retry warning chip in default chrome, rate_limit_event nested rate_limit_info parse + overage chrome + session-level hoist on rejected). Closes WIKI-211/212/213/214/215. 2 rounds.
- WIKI-172 merged c604ad3 — merge-ready loop UI: ticket-level chrome (round N/8 subdued/warning/danger tiers, unrouted verdict badge {0,1}, plateau counter w/ first-line normalization + similarity, expandable history panel via canonical workgraph edges), backend loop_state.py derivation, cap chain graph→template→default (F4 rejection upheld, WIKI-164 doctrine). 3 rounds.
- WIKI-171 merged c713e87 — next-review orchestrator primitive: single MCP tool + endpoint collapsing gate + worktree + spawn + prev-archive per merge-ready round (crash-safe journal-before-side-effect w/ archive reconciliation, status-file terminal-state derivation, short-SHA canonicalization, AGENT_STATUS_DIR + operations-journal test isolation, MCP schema shared w/ NextReviewIn, cc-optional effort). 4 rounds.
- WIKI-170 merged 0f1493b — fleet-wide DAG rollup: /fleet/graph endpoint aggregating live workers + last N archived per orch via graph_health loader (bounded metadata-first slice, headless orch bucket seeding, endpoint classification via node_tickets for legacy N-* nodes), frontend fleet-graph.tsx w/ orch groups + client-side filter chips. 3 rounds.
- WIKI-169 merged 63542b9 — workgraph timeline scrubber: per-ticket snapshot list endpoint (metadata-only, sidecar-persisted at write time), graph-at-revision endpoint w/ closest-older fallback, frontend slider + play/pause + AbortController on rapid scrubs. 2 rounds.
- WIKI-167 merged 4f9ab19 — compact Mermaid preview readability: swapped scale-to-fit to native-size cropped preview w/ visible hover/click affordance; swapped --interactive-accent (undefined in all themes) to canonical --accent-primary; added computed border/ring regression w/ mutation proof + gruvbox-light screenshot. 2 rounds.

## 2026-07-27

- WIKI-165 merged 137bd45 — FleetMonitor graph_health integration (composite-health detector, typed escalation appends, snapshot fallback, durable episode markers, centralized role normalization); extracted graph_health.py + ticket.py modules; canonical end-to-end restart regression. 4 review rounds.
- WIKI-163-P3 merged 977bd6b — divergent-newest orphan test now load-bearing (spawn request_id differs from hot), bounded-read + invalid-orphan fallback + two-crash convergence tests added; 3 review rounds
- phoebe PR #12483 merged: conversation search relevance ranking fix (PHO-14590, luna worker, 2 sol review rounds)
- phoebe PR #12482 merged: admin agent dock chat transcript scroll root-cause fix (DOCKSCROLL-1, fable-5 worker, sol gate clean)
- **wiki** — WIKI-166 workgraph sidecars excluded from agent listings — phantom-worker fix ([#129](https://github.com/hwang2409/wiki/pull/129) merged ee887fe, 1 clean sol round, mutation-verified regression test)

## 2026-07-24

- PHO-14367 merged: accounts page IA overhaul — KPI tiles, 6 tabs, progressive disclosure, attio/granola meetings + linear tickets surfaces w/ bounded minimized reads, org-tz dates (#12186 squash bf57d589, 3 rounds, stacked on 14248 then unwound)

## 2026-07-23

- PHO-14248 merged: agent-first chip integration (org + agent_trace chips w/ structured facts, causal preload hints, accounts context provider w/ bounded summary + lazy sections, read-back revalidation) + PHO-14242 accounts rework fold-in (#12173 squash 0ff93712, 4 rounds)
- PHO-14247 merged: agent-first admin dock shell + Linear-style footer (#12066 squash 7c7e1743, 11 review rounds + Henry-directed fable footer redesign, variant A — dock/tabs/chips/persistence, activity-ordered footer previews, full race-regression package, screenshots-on-PR flow)
- PHO-14250 merged: agent-first admin chat-vs-page skill doctrine + golden evals (#12103 squash ab016a66, 5 rounds — honest Linear fixtures via production result model, Henry-approved user_overrode audit field, un-droppable baseline preload, exact caregiver deep links)
- WIKI-164 graph-engineering D3 templates + selector merged (#128 squash 43c80b3, 2 rounds — 6 doctrine templates, template schema in wiki graph lint, select-template ticket-prefix resolver w/ repo.implement fallback, trigger-DSL deferred)

## 2026-07-22

- PHO-14238 merged: admin agent scratchpad frontend vault UI + read-only API (#12058 squash 384a01a5, 8 rounds — contracted artifact route w/ redirect, canonical scope URLs, filter-bar uniformity per Henry screenshot; known residual: 2 new round-trip tests assert mock.calls[0] without clearing, fold fix into next PR touching use_scratchpad_index.test.ts)
- WIKI-163 graph-engineering D2 workgraph + renderer merged (#127 squash fd92d5f, 5 rounds — canonical-action edge wiring, snapshot-before-hot + orphan reconciliation under per-ticket lock, request-ID replay idempotency keyed by mode, lifespan-drained keyed outbox, injected clock, /agents/<T>/graph live+replay)
- PHO-14260 merged: haiku-4.5 tier for admin agent mechanical map subagents (#12112 squash cb083488, 5 rounds — prose classifier killed for server-validated mechanical_contract, ~67.3% cost cut on that traffic, cross-provider fallback intact)
- WIKI-162 graph-engineering D1 schemas + linter merged (#126 squash c06678f, 5 rounds — steer-path Steer validation, nested SHA binding, frozen-sidecar schema bundling + real PyInstaller MCP smoke, per-error required-key pointers)
- PHO-14272 merged: admin agent graceful worker handoff + 48h interrupt-cause rollup (#12105 squash 736a0101, 4 rounds — shutdown-event-gated cancellation attribution, coordinated-deploy runbook w/ exact scalar query, lease-fenced idempotent recovery)
- Corey admin-agent access fixed: users.admin was already true; real blocker was org_slack_user_mappings user_id NULL in Phoebe Home Care (link click had bound Orchard St. row instead). One-row prod UPDATE linked U0AGES89BLZ to ebcca7d3 (approved by Henry); Orchard St. row left as-is per Henry
- PHO-14285 merged: llm_framework anthropic defer_loading + tool_search_tool_regex_20251119 opt-in support (#12095 squash 62bc583a, 3 review rounds) — unblocks PHO-14286 eval harness
- **wiki** — 07-22 quad-merge regression sweep fixed inline on main (3db94ae — sidebar orch->worker grouping restored, Cmd-K palette 14s->0.2s artifact-cache, visible tool-reference markers, synthetic-row spacing, composer echo-lag dedupe)
- PHO-14258 preload closure widening merged (PR #12072 squash 748f4a4a, 5 sol rounds — classifier payload fix, multi-workflow closure, fallback budget, underscore-boundary regex, registry-derived mutation guard)
- WIKI-156 artifact shell + renderer polish merged (#125 squash 316e015, 3 rounds)
- WIKI-161 synthetic-source system messages merged (#123 squash c58b319, 4 rounds)
- WIKI-151 nav + sidebar IA merged (#124 squash fdf77f0, 3 rounds)
- WIKI-148 composer slash menu merged (#119 squash 33c951a, 9 rounds)
- PHO-14264 admin agent tool-discovery design + gated prototype merged (PR #12080 squash c0e3bdee); shipping chain PHO-14285..88 filed; PHO-14285 framework-prerequisite worker spawned (cdx luna)
- **wiki** — WIKI-147 session-list unread dot — durable per-run viewed store keyed by run_id w/ 404 on unknown + OS flock monotonic writes; startup-baseline snapshot w/ per-run asyncio.Lock (concurrent first-writer safe); NULL-baseline for post-deploy runs; real-event freshness via supervisor SSE (no mtime hack); bounded 3-attempt exp-backoff retry w/ failed-state UI + one-controller coalesced follow-up POST; a11y unread label — PR #121 squash eb3e087f (8 review rounds + post-rebase R9)
- **wiki** — WIKI-153 transcript debug-detail polish (first WIKI-150 census batch) — shared BoundedPreview primitive (bounds/line+byte count/copy-full/wrap); tool-call block failed label + input via preview; bash result 3-section command/output/error w/ ANSI preserved; hook-message-registry whitelist (model_changed included) w/ unified info/warn/error style + non-whitelisted collapsed to run details; action-required card default question+choices + details disclosure; GitHub-preview segments clip + ANSI-parsed-first ordering preserving SGR spans + OSC-8 — PR #122 squash c623d640 (6 review rounds; rebased after WIKI-144/146/149 landed)
- **wiki** — WIKI-146 command palette (Cmd-K) — fuzzy search across sessions/tickets/artifacts/vault notes; durable panel/artifact/focus URL surviving refresh; symlink-safe vault walk w/ per-file mtime cache + atomic publish + server-side cancellation; ranked-subsequence scoring w/ bounded recency tie-breaker; focus trap + inert background + invoker-focus restore; capture-phase Cmd-K in composer/editor/terminal; all 6 ticket prefixes route to Linear fallback — PR #120 squash 304066d (4 review rounds; rebased after WIKI-149 landed)
- **wiki** — WIKI-149 code-block copy button + kind:diff artifact renderer — state-aware unified-diff parser (multi-file, bare headers, hunk-content `-- `/`++ ` safety, binary/rename/mode-only headers, `\ No newline at end of file`), per-artifact line-number toggle via ArtifactViewState, scrollable viewport w/ final-line visibility, WCAG-AA copy button, `@media (hover: none)` touch reachability — PR #117 squash 57c00819 (4 review rounds; rebased on origin/main after WIKI-144 landed)
- **wiki** — WIKI-144 badge sweep — unified StatusBadge across dashboard/session-header/artifact rows/artifact-panel file-list; centralized statusToTone; WCAG-AA-compliant compact-badge token + contrast assertion; real-surface playwright — PR #118 squash f5214ad (2 review rounds)
- **wiki** — WIKI-145 UI refresh II — 12 audit treatments (narrower rail, transparent workspace-sidebar+tab-header, hairline tabs, left-accent active row, borderless session-header, plain-prose assistant, downgraded notice/composer/activity-head, text-toggle chips); honest screenshot fixture + resilient python resolver; new layout/noise/typography playwright suites — PR #116 squash 7be5561 (3 review rounds)

## 2026-07-21

- **wiki** — dropped provider auth ribbon badge (b21bc5c) — sidecar probe stuck unknown, chips pure noise; backend probe kept
- **mitm-inspector** — MITMWEB-B4+F7 flow-summary enrichment, body search, conversation view merged (tooling PRs #2, #3, 2026-07-20) — restored after vault rollback
- **mitm-inspector** — MITMWEB-F8 session-first UX merged (tooling PR #5, squash cd1430b) — session-list home, canonical-thread chat drill-in with manual picker, sanitized markdown; 12 sol review rounds + in-session fallback pass
- **mitm-inspector** — MITMWEB-F9 readability polish merged (tooling PR #6, squash b117f92) — two-line session rows, centered 76ch conversation column, role-distinct turn cards; presentation-only
- **mitm-inspector** — MITMWEB-F10 chat-UI overhaul merged (tooling PR #7, squash 7ed5ef6) — Henry kill-list applied (auxiliary-calls collapse, side-call tables, default-visible picker), breadcrumb drill-in header, 860px transcript column
- **mitm-inspector** — MITMWEB-F11 chat render fidelity merged (tooling PR #8, squash dbe26bc) — remark-gfm tables/strikethrough/task-lists, untrusted-tag escape via mdast html→text nodes, wider clamp(960px,92vw,1320px) transcript
- **mitm-inspector** — MITMWEB-B5 backend read-path perf merged (tooling PR #9, squash a5a6cb4) — flow-index detail reads, skip redundant re-validation; packet loads 11.6s/30s+ timeout → <100ms
- **mitm-inspector** — MITMWEB-B6 websocket browse path removed merged (tooling PR #10, squash dac2808) — sqlite-backed GET /sessions + /sessions/<id>, SPA on HTTP reads, WS stream/fanout/cursor deleted
- **wiki** — Moxy Static font bundled + added to mono font picker (5bbb71b, direct commit)

## 2026-07-20

- WIKI-136 dashboard j/k scrolling (#108 squash c3c3278, 1 review round + post-merge sync, patch-id verified)
- WIKI-134 C-a leader keybind regression fixed (#107 squash 1283ed0, 2 review rounds)
- WIKI-137 frontend cache headers (#109 squash ce4216f)
- WIKI-140 dashboard project/date/state filters (#110 squash 14cd5b4)
- WIKI-139 provider auth health probe + badge (#112 squash 450dd47)
- WIKI-138 supervisor-native fleet monitor — pushes worker state transitions to orchestrators (#111 squash f049c8e)
- PHO-14067 churned-org DRI staleness filter merged (#11795 squash 4aa28e53, 1 sol round clean)

## 2026-07-19

- PHO-14061 agent-correctable admin tool validation errors merged (#11764 squash b9371707, 4 sol review rounds)
- PHO-14060 exception-noise cleanup merged (#11763 squash 236463b2, 1 sol round clean)

## 2026-07-18

- PHO-14053 wellsky clock-out notify-once + stale-date recovery merged (#11747 squash 0fae1222, 4 sol review rounds)
- PHO-14047 DRI staleness auto-join + taxonomy fix merged (#11745 squash 946ba0c9, 3 sol review rounds)
- PHO-14046 vendor retry observability fix merged (#11744 squash a72d5435, 2 sol review rounds)
- mitm-inspector — MITMWEB-S1 durable SQLite persistence merged to local main@07f2b53 (candidate bfd092a, final clean sol review round 8; 577 pytest + 207 vitest + lint/type gates green on the clean merge tree)
- phoebe — PHO-14029 people dedup merged (#11739 at 2026-07-18 06:16Z squash 323c75bdd4; 6 sol review rounds; canonical-row consolidation + lower(email) unique index + dry-run/apply CLI; prod apply still pending Henry)
- phoebe — PHO-14003 capabilities page refactor merged (#11688 at 2026-07-17 19:56Z; Skills+Tools tabs, 3-column minimal table, source_file_path backend field)
- phoebe — PHO-13989 S3 spill tier for admin artifacts merged (#11671 at 2026-07-17 22:14Z; inline jsonb under threshold, S3 above; hydration round-trips + DB spill persistence tests; review rounds cdx/sol)
- **phoebe** — PHO-13944 PR #11630 merged at 0e40d33efc; weekly feature-catalog sweep shipped; classifier edge cases deferred to PHO-14034

## 2026-07-17

- MITMWEB-F6 density+right-panel merged to main@f3ab43a (candidate be11314, no review round — Henry live-iterate mode; cc/opus-4.7 implement; right-side inspector dock, kill body-panel chrome, uncap frontend decoder to wire ceiling, raise backend prefix default, JSON tree default view)
- MITMWEB-F5 minimalist b/w merged to main@ba80c53 (candidate 874280f, 3 review rounds cc/opus-4.7 implement + cdx/sol review; light-default palette, JSON tree, SSE frames, /v1/messages summary, useSeenFlows hook, palette allowlist)
- MITMWEB-F4 restyle merged to main@e0f474b (candidate 7da535b, 4 review rounds cdx/luna implement + cdx/sol review; wiki-app tokens/shell/flows/inspector + Playwright e2e responsive pin)
- WIKI-132 merged as https://github.com/hwang2409/wiki/pull/105 (6c52a60a squash, 5 review rounds)
- MITMWEB-I1 merged to main@8e73838 (candidate fe2b8e7, 3 rounds cdx/luna implement + cdx/sol review)
- WIKI-133 merged as https://github.com/hwang2409/wiki/pull/104 (643628b2 squash)
- PHO-13986 sandbox allowlisted egress merged (#11669: pypi-only, reviewed-constant governance, literal-policy test — 2 sol rounds; Henry manual approval cleared skipped auto-approve)
- PHO-13987 dana mode merged (#11676: data-analysis skill + stats guardrails + phoebe chart style w/ traceable UI tokens — 1 clean sol round)
- PHO-13983 matcher fixes merged (#11664: own-domain exclusion, attio type-separated associations, contamination cleanup — 2 sol rounds + diff verify). PHO-13988 generalized inline artifacts merged (#11670: type@version registry, chip fallback — 2 sol rounds).
- PHO-13979 skill overrides auto-apply merged (#11659, 2 sol rounds: DB-level append-only audit trigger + RESTRICT attribution; /admin/agent-skills page removed)
- PHO-13980 inline calendar artifacts + hover merged (#11656, 2 sol rounds + orchestrator diff-verify; scope-creep AGENTS.md edit caught and reverted)
- PHO-12880 worker archived (PR #11548 stays open, parked for lead discussion; worktree + branch retained). Slack admin-bot channels:join reauth + persona identify check completed by Henry.
- MITMWEB-B3 merged (mitm-inspector main@2bef0c8, candidate 21e98c7, 2 review rounds): loopback HTTP+WS protocol-v1 delivery (snapshot/delta/cursor/gap/resync), JSONL unix-socket ingest, readiness probes, live CLI wiring; Review1 MEDIUM (close() hang on live connections, py3.12 wait_closed) fixed + empirically verified. Wave 2 complete — all foundational tickets merged.
- MITMWEB-F2 merged (mitm-inspector main@b0dee31, candidate 8c99d3c, 1 review round): virtualized 28px flow grid + mitmproxy-compatible filter language + follow-live/selection workspace wired to F3 inspector; 133 vitest, Playwright desktop+mobile, all gates green on merged main. 4 LOW findings deferred to I1 (flow-limit boundary test, refreeze O(512x32) hot path, ~t param fidelity, lifecycle truncation marker).
- WIKI-131 fixed (e5370de): chat tables now horizontally scrollable — session markdown was missing the table scroll-wrapper override
- MITMWEB-B2 merged (mitm-inspector main@712df98, candidate 9a1d1dc, 15 review rounds): capture store/adapter/sequencer/sink hardened; final fix = discarded-flow guard in _refresh_state_weight killing phantom metadata recharge; suite+ruff+mypy green on merged main. Arc survived codex quota exhaustion (locked to Jul 23) + 2 dead orch/worker processes.
- PHO-13957 run_admin_python_snippet retired, sandbox-only run_code_in_sandbox merged (#11637, 4 sol rounds: reap-replay, per-invocation namespacing, path canonicalization, caps-pipeline routing)

## 2026-07-16

- PHO-13950 stub previews + sandbox hydration merged (#11594, 7 sol review rounds — caps invariant saga: node-cap loss, cell-cap bypass, UUID corruption, key-spoofing all regression-locked)
- PHO-13944 WS3 feature catalog PR #11581 merge-ready, worker archived, handed to Henry for manual merge (PR 2 auto-apply weekly sweep still unbuilt)
- PHO-13943 Granola-org matching + agent meeting-context tool merged (#11580, 3 sol rounds / 3 blockers+6 majors: free-mail linking, override leakage, unwrapped evidence, write-by-default backfill, O(NxM) sync, atomicity) — PHO-13940 workstream 1 done; post-deploy: run match-backfill --write against prod Core
- CORE-BODY-UUID-HOTFIX merged (#11623): prod TypeError on admin Linear-ticket creation (DEP-108 idempotency_key UUID) — root fix mode=json in CoreRequestModel.core_body, orchestrator-authored + worker-shipped
- gauge — GAU-4 CLI + wiring merged (terminal charts, grapheme-safe width, query API wired, 5 sol rounds, master@9d32f0b) — v0 COMPLETE, success criterion verified live
- PHO-13884 /admin/agent UI reimagine merged (#11542, cc/fable worker: Wiki-style progressive disclosure, settled-turn promotion, purple identity; 4 sol rounds + 4 Henry live-feedback rounds; shared chat_view changes gated behind groupActivityEntries prop)
- gauge — GAU-2 scraper merged (reqwest hardened client, deadline cadence, target identity, staleness, 4 sol rounds, master@bf472be)
- gauge — GAU-3 query engine merged (expr parser/eval w/ depth+point budgets, counter-aware rate, mountable API, 3 sol rounds, master@1646ec8)
- gauge — GAU-1 storage engine merged (bit-packed Gorilla, WAL, atomic partitions, unlink-safe indexed reads, corruption hardening, 7 sol rounds, master@444a60c)
- DEP-108 Linear-Core accounts + ticket creation merged (#11487, dash's PR + our fixer through 7 sol rounds: dup customers, idempotency redesign to stateful attempt machine, classification table, status-first parsing) — unlocks PHO-13940 workstream 4
- PHO-13938 persona in posthog.identify merged (#11567, orchestrator direct-read review — diff exactly to spec)
- gauge — GAU-5 exporters merged (gauge-node + gauge-proc, nonblocking cached scrapes, panic recovery, 5 sol rounds, master@bd094bf)
- PHO-13882 dynamic skill editing merged (#11533, 2 sol rounds: hostile-draft diff DoS + per-skill approval race caught/fixed) — agent proposes skill-body drafts, human approves in admin panel, versioned rollback
- PHO-13898 sandbox transport merged (#11535, 2 sol rounds: lying-truncated-flag trust, ExeDev prompt mismatch, image-param contradiction, artifact spill) — matplotlib-in-chat pipeline live: analysis image + base64 reads + admin.image@1 artifacts
- PHO-13911 worker-admin pool PARKED by Henry: app-side PR #11541 mergeable after 2 sol rounds but deliberately closed; branch preserved at 952171cf35; handoff context in ticket for Henry's lead
- pufferclone — PUF-11 memory budget + LRU eviction merged (drain-aware policy, serialized accounting, loader extraction, 3 sol rounds, main@6bc5d64) — v2 COMPLETE
- Snowflake IAM hotfix merged by Henry (#11534, TF secret shell + grant, sol MERGE-READY, security assessor high = manual approval path)
- pufferclone — PUF-12 puf CLI merged (thin HTTP client, certainty-tracked batch upserts, hybrid query, source-aware abort accounting, 6 sol rounds, main@d784b30)
- Prod Snowflake migration incident closed: root cause = deploy job authored with secret existing only in dev + no IAM grant (latent since birth, continue-on-error masked). Fix: TF secret shell + grant (Henry applied), key copied dev->prod, job rerun SUCCESS — #11491 QHS loaders live in warehouse. PR #11534 pending 2nd human approval (security assessor rated high). Follow-up candidate: separate prod Snowflake keypair
- tix — TIX-1 v0 merged (ticket tracker CLI: atomic durable JSON storage, flock mutations, O_EXCL ID allocation, 6 sol rounds, master@9524a17)
- PHO-13881 admin agent tools/ subpackage move merged (#11527, 2 sol rounds: incomplete move + orphaned test + purity noqa caught then fixed; barrel exports byte-identical)
- pufferclone — PUF-10 benchmark suite merged (criterion + 100k rows, deterministic admission probe, recall harness, BENCHMARKS.md, 2 sol rounds, main@f5abeec)
- Modal sandbox backend ACTIVATED: #11522 merged (.env.keys injection) + CODE_SANDBOX_BACKEND=modal set in staging+prod phoebe-app-env-vars (verified single-key delta, AWSPREVIOUS rollback); staging live on autodeploy, prod on next deploy
- pufferclone — PUF-9 MinIO end-to-end merged (compose fixture, gated s3_e2e, hardened smoke, 2 sol rounds, main@58436ca)
- PHO-13826 live Modal smoke PASS (dev env): full verb cycle create/exec/write70KB/read-roundtrip/list/destroy + egress blocked; stdin write fix verified against real Modal; activation gate = CODE_SANDBOX_BACKEND=modal
- PHO-13826 Modal sandbox backend merged (#11447, 3 sol rounds: silent param drop, 64KB argv limit, stdin-heredoc empty-write regression) + PHO-13855 Slack admin agent self-join merged (#11472, 4 sol rounds converging 3maj->1maj->1min->clean; needs manual admin-bot Slack reauth for channels:join) — both admin-merged by Henry auth via phoebe orch

## 2026-07-15

- PHO-13826 PR #11447 round-2 write_file fix pushed at c8f8ab6a1890885f50381449fbd046c18f4eb290; Modal script now uses python3 -c with stdin content and 64KiB read-back regression passes
- PHO-13826 PR #11447 refreshed onto origin/main at 18c8eef6fd3fec1e00780ff8ba285ded69655556; CI green, awaiting round-2 review
- pufferclone — PUF-8 non-blocking cold loads merged (bounded admission, lifecycle coordinator extraction, cancellation-safe finalizers, 5 sol rounds, main@87cad8d) — v1 COMPLETE
- WIKI-117/118 merged (#103, squash 8f68d53): artifact test env isolation + #91 hardening; 4 review rounds (sol), mutation-verified
- WIKI-130 deployed: supervisor graceful stop -> atomic swap -> app relaunch verified (repo=/Users/henry/me/fun/wiki, API 200); fleet auto-resumed (5 codex + 3 claude under new supervisor); stacked 127/116/129 backend changes now live
- WIKI-130 fix committed (1b1cacb): resolve_repo_dir escapes .native-build-staging like .codex; 3 Rust unit tests added (first in src-tauri); staged bundle built, swap pending fleet-idle
- PHO-13830 live EHR record fetch merged (#11441), PHO-13832 shift classification calendar merged (#11444), PHO-13827 sandbox run ownership merged (#11445) — phoebe orch batch merge for Henry; workers respawned for PHO-13826 (#11447 dirty+CI red) and PHO-13763 (draft #11383 resumed)
- GAZELLE-HOTFIX merged as https://github.com/phoebe-health/phoebe/pull/11455 (main unred: 11342 gazelle drift)
- **pufferclone** — PUF-6 segment compaction merged (newest-wins, tombstone lifetime, crash-safe, 2 sol rounds, main@870292b)
- WIKI-129 merged (#102, 4 rounds): native-build safety — GUI-lifetime app.lock, guard+swap hold locks through atomic RENAME_SWAP, staged out-of-place builds, PID-reuse/stale-lock safe, second-instance clean exit, interrupt-safe sentinel
- WIKI-116+124 merged (#100, 5 rounds): artifact render trust — render-error feedback loop (durable dedupe, injection-safe normalized diagnostics, truthful delivery states) + SVG root-geometry parse fix (descendant width/viewBox no longer shrink previews)
- **pufferclone** — PUF-5 hand-rolled HNSW merged (incremental insertion, two-heap search, filtered beam, 3 sol rounds incl. full rebuild directive, main@fa274a9)
- **pufferclone** — PUF-7 S3Store merged (conditional-put write-once, conformance suite, 3 sol rounds, main@21951c8) — MinIO-ready for local-cloud
- WIKI-112 merged (#101, 2 rounds): dead-archive flake root-caused as test rot — fixture never persisted runtime states to durable registry; fix = persist snapshot + deterministic precondition wait; 20/20 greens
- **pufferclone** — PUF-4 engine+API merged (3 sol rounds incl. 7-major xhigh round); v0 COMPLETE main@3ee62c5
- pufferclone v0 complete: PUF-1..4 merged, main@3ee62c5, 46 tests, HTTP smoke green
- WIKI-127 merged (#98, 6 rounds): multi-workspace file browsing — fd-pinned roots + constant-descriptor tree walk, workspace derivation from orchestrator registry, file://<workspace>/<relpath> path space, recents v2 migration
- orchard@phoebe.work provisioned prod (f5025027) + staging (273fa49a) — account-health tick unblocked
- WIKI-128 merged (#99): code-viewer density fix — view-content prose leak removed from code scroll container, line-height 1.4 both render paths, h-scroll restored, tighter gutter
- PHO-13815 merged as https://github.com/phoebe-health/phoebe/pull/11433 (review-marker backfill from Core + false-ping cleanup; 2 sol rounds)
- **pufferclone** — PUF-2 index modules merged (exact-scan vector, BM25 mergeable stats, filters, RRF, 3 sol rounds, main@2068d11)
- **pufferclone** — PUF-3 segment container merged (crc32, opaque sections, 2 sol rounds, main@a226022)
- PHO-13804 merged as https://github.com/phoebe-health/phoebe/pull/11420 (admin agent full Intercom messages; 4 sol rounds + 2 Bugbot rounds)
- **pufferclone** — PUF-1 foundation merged (types/store/WAL/manifest, 4 sol review rounds, local repo main@c2aa070)
- ORCHARD-EMAIL merged as https://github.com/phoebe-health/phoebe/pull/11431 (account-health service email → orchard@phoebe.work)
- WIKI-121 CodeMirror 6 editor merged (#95): markdown+lazy langs, CSS-var theme, history/undo, code-split; 6 review rounds incl. fallback design simplification
- WIKI-125 note rendering merged (#97): mermaid (lazy/strict/theme-synced), vault asset endpoint, image/svg embeds, table styling; 4 review rounds
- WIKI-122 explorer polish merged (#96): extension icons, repo files + boundary fuzzy in Cmd+K, Recent group; 4 review rounds
- **wiki** — WIKI-123 repo-scoped file browsing merged (#94, 0b6e9ad) — file API re-rooted at repo, 2-round sol gate
- PHO-13783 analysis-log note Slack automation merged (#11397, 02f633ea47)
- PHO-13780 account-health tick cron registered + barrel-registration guard merged (#11398, 0e4974121f)
- PHO-13762 DRI staleness Slack pings merged (#11385, d308fa37e9)
- PHO-13760 deployment report pack merged (#11384, 4 gate iterations)
- **wiki** — WIKI-119 non-markdown file open — scoped file API + code viewer pane merged (#93, 0b6f391); 3 gate iterations, sol caught symlink-alias/TOCTOU class issues round 1, descriptor containment + exact-cap fixes round 3

## 2026-07-14

- PHO-13733 admin tool surface docs merged (#11375, 5 gate iterations, PHO-13759 spun out)
- WIKI-120 merged (#92): terminal fidelity (batching, safe PTY spawn, binary WS w/ capability negotiation, single-owner fd) + Cmd+F scrollback search. First full Fable→luna→sol pipeline run: 3 iterations, sol caught 4 BLOCKING (fork-in-threads, parent-PGID kill, frozen-build dispatch, fd-reuse races) + 2 HIGH; subsumes wiki-43/87cf014
- PHO-13736 customer-predicate word-boundary regex merged (#11364)
- PHO-13735 lifecycle_state_series cohort_anchor_date fallback merged (#11365)
- WIKI-115 merged (#91): cdx render_artifact mcpToolCall results now normalize to kind=artifact events (write-time + read-time heal for archived runs); gate found WIKI-117 (test env-sensitivity) + WIKI-118 (hardening bundle); needs native-build+relaunch
- [WIKI-114](https://github.com/hwang2409/wiki/pull/90) legible compact preview for large mermaid/SVG artifacts — explicit px dims from viewBox (kills Chromium 300px fallback), 400px crop + bottom/right fades + click-to-inspect via existing pan/zoom. Gate: 2 iterations (2 BLOCKING: wide diagrams still 2-6px text via width:max-content fallback, viewBox-only SVGs collapsed 0x0; 1 HIGH: test passed by 276px-fixture coincidence — wide fixture + width assertions added). Merged e792f49 via #90.
- **phoebe** — admin account note -> org-Slack admin-agent thread automation merged (#11339, no ticket yet — backfill after Linear re-auth)
- **phoebe** — PHO-13685 point-in-time lifecycle state-series primitives for churn cohort/survival merged (#11336)
- **phoebe** — PR-11284 callout 2-min timing check + ALL-group grace race + DB-clock cutoff fixes merged (#11284)
- PHO-13690 merged #11325
- PHO-13676 merged #11310
- **phoebe** — PHO-13676 admin-agent in-flight tool-call dedup black hole fix merged (#11310); PHO-13690 atomic lifecycle CSV export tools merged (#11325)
- [WIKI-88](https://github.com/hwang2409/wiki/pull/89) orchestrator wiki-artifacts MCP registration — subsumed into WIKI-111 (#89): role gate lifted, WIKI_RUN_ID isolation regression-tested
- [WIKI-111](https://github.com/hwang2409/wiki/pull/89) built-in agent harness — WIKI_RUNTIME_CARD v1 injected into every cc/cdx run (role-flavored, 4KB budget), wiki agent spawn/status/steer/replace/archive CLI verbs + backend URL autodiscovery (fixes watch dead-port), orchestrator MCP fleet ops (role-enforced at list+call), WIKI-88 subsumed (orchestrator MCP + WIKI_RUN_ID artifact isolation). Gate: 1 iteration, 0 BLOCKING/HIGH, 1 MEDIUM deferred to WIKI-113. Merged 14e78d2 via #89.
- [WIKI-109](https://github.com/hwang2409/wiki/pull/88) Cmd+K session search + blank-pane placement (WIKI-110 folded) — sessions first-class palette results (dead/archived excluded), C-a p blank pane, move-only-from-blank semantics with pane-state key retention. Gate: 1 iteration + rebase over #87 (1 MEDIUM: palette selection reset under SSE churn). Merged 58c6c30 via #88.
- [WIKI-108](https://github.com/hwang2409/wiki/pull/87) sidebar pages open in separate windows — utility:// pane identities, reuse-and-focus, standalone restore; settings modal untouched. Gate: 1 iteration (1 MEDIUM: npm test chain registration). Merged 47cddb0 via #87.
- PHO-13669 merged (#11303, 17d7241ae6): account-book name filter (case-insensitive, projection-preserving, 100-row name-ordered scan) + HTML-only email bodies -> bounded redacted text (bs4) + embedded opening-marker neutralization in wrap_untrusted_evidence + account-health prompt diet
- PHO-13666 merged (#11311, 89c9bdbc47): source_health.org_sync_history curated query (30d-bounded CTEs after 74k->21k cost evidence) + source_health cross-listed under ehr domain
- PHO-13665 merged (#11307, cfe22ef087): investigation-context resolver exact-name matches uncapped to 50 + truncation signals (fixes silent 5-of-13 SYNERGY enumeration)
- [WIKI-104](https://github.com/hwang2409/wiki/pull/85) supervisor RPC responsiveness — /api/agents served from registry snapshot (no supervisor round-trip, <1s under load), idempotent spawn/message via request_id (errors cached too; half-landed-spawn retry safe), fast-read-allowlist timeouts + may-have-succeeded error wording, WIKI_WORKER_SOFT_CAP=5 warning. Gate: 3 iterations (3 BLOCKING incl. int-request_id 400ing codex approvals + replace/stop timeout regression; then control_attached pid-inference vs types.py doctrine + pid-reuse suite failure — root-caused via daemon-truth attachment snapshots + detach grace). Two false green-suite claims caught; final suite independently verified 2x346 OK. Merged via #85.
- PHO-13675 merged (#11309, b10eafeed3): rollup cohort-chunk input (kills 27-min UUID transcription; server-resolved eligible_organizations chunks, resolved-org audit scope), end-exclusive window docs + corrective errors + input_validation category; pulse prompt uses cohort chunks
- PHO-13667 merged (#11308, 04e8c38768): trailing_outreach_metrics 7d windows (audit-backed 4.7k cost), pre-call window validation w/ input_validation category + org-scoped denial audits, previous-period docs; SMS stays 1d (162k at 7d)
- PHO-13668 merged (#11304, f3b130f83e): query_admin_database timestamp min/max aggregates + closest-match domain suggestions + alias-collision guard
- [WIKI-105](https://github.com/hwang2409/wiki/pull/86) font weight picker — per-surface weight selector (interface/note/mono) with canvas-measure real-weight detection (probes 100-900, dedupes faux-bold, hides single-weight families), CSS vars only set when user selects (default rendering unchanged), ui-state persisted + cross-window mirror. Gate: 2 iterations (2 BLOCKING: modal overflow visible in worker's own screenshots, :root defaults changed unselected rendering; + 1 HIGH modal-width drive-by, 3 MEDIUMs — all fixed, re-screenshotted, live-measured). Merged via #86.
- PHO-13664 merged (#11299, 7befa37d40): composite context tool budgets 200ms/500ms -> 2s/5s (EHR state) + 2.5s/8s (context index + summary), failure isolation preserved, budget-floor regression tests; tools stop being prod no-ops
- [WIKI-103](https://github.com/hwang2409/wiki/pull/84) orchestration ergonomics — wiki agent watch (deduped events, stall detection, --until merge-ready|terminal|merged, NDJSON), wiki gate (one-call checks+threads+SHA verdict, exit codes), todo complete no longer dumps full body into done.md (--done-line flag). Gate: 3 iterations (1 BLOCKING --until-merged hang, path-traversal MEDIUM, dedupe gaps, then 1 HIGH remote-fallback regression introduced by fix — all fixed + revert-tested). Merged 38cbc8d4.
- [PHO-13662](https://linear.app/phoebework/issue/PHO-13662): composable python-snippet pipelines (named outputs, artifact listing, re-run) — cdx:PHO-13662 (gpt-5.6-terra)
- [PHO-13660](https://linear.app/phoebework/issue/PHO-13660): deterministic multi-signal account-health tiering in rollup + pulse prompt cleanup — cdx:PHO-13660 (gpt-5.6-terra)
- PHO-13662 merged (#11288, 6fff4b07ac): composable admin python-snippet pipelines — named step outputs, list_python_snippet_artifacts, hash-pinned reruns, step_name refs in run-data verbs; sandbox threat model unchanged
- PHO-13660 merged (#11285, 51032abc02): deterministic multi-signal account-health tiers in batch rollup (union recency + WoW collapse + EHR pipe + 21d grace), two-phase whale-safe recency, slack_fields machine-wired, Snowflake prompt residue purged, flag-skip log; validated on prod (150 orgs: 114/16/20)
- [WIKI-102](https://github.com/hwang2409/wiki/pull/83) knowledge index diet — event chunks excerpt-clipped to 2048 chars (notes stay full), ingest filters strip base64/ANSI LINES (not whole events, post-gate fix) with per-line counters, legacy agent-archive sweep now opt-in (--include-legacy, default off), SCHEMA_VERSION=2 drop+rebuild migrates. Fixture size bound <25%; real-corpus rebuild pending next reindex. Gate: 2 iterations (1 MEDIUM recall hole + 2 LOW counter defects, fixed + revert-tested). Merged f42d9111.
- [WIKI-100](https://github.com/hwang2409/wiki/pull/82) knowledge layer v1 — rebuildable SQLite index (~/.wiki/knowledge.db, WIKI_KNOWLEDGE_DB_PATH override): FTS5 over vault notes + wiki-managed run events, wikilink graph (backlinks/orphans/unresolved), `wiki search`/`wiki links`/`wiki index rebuild` CLI + search_knowledge MCP tool; ingest async off hot path (vault mtime+hash delta, archive hook, lazy live-run by seq); budgets asserted (query p95 <50ms, rebuild <60s fixture). Gate: 2 iterations — full review found 1 BLOCKING (query-path live-indexing failure bricked search) + 1 HIGH (one corrupt archive halted all ingest) + 1 MEDIUM (corruption-recovery unlink race), all fixed + revert-tested; graceful-degradation paths verified live post-merge (lock contention -> stale/partial results). Merged 0818766b. Spec: docs/superpowers/specs/2026-07-14-knowledge-layer-storage-design.md
- [WIKI-101](https://github.com/hwang2409/wiki/pull/81) cc render_artifact rejected-badge fix — dropped structuredContent from MCP success results (sentinel now reaches cc transcripts); parser fallback reconstructs artifact event from tool input on bare {artifact_id,ok:true} results with full server-parity validation (kind allowlist, UUID, TEXT_LIMIT, title/caption limits, image ref-shape not data_base64); e2e now asserts the RESULT parses into a kind:"artifact" session event for BOTH providers (closes WIKI-89 assertion gap). Gate: 1 steer iteration (2 MEDIUMs fixed, red-before-fix verified). Merged 34eb99a9.
- PHO-13646 merged (#11271, 00eac05ab1): admin agent batch account-health rollup tool (get_admin_account_health_rollups, ≤50 orgs/call, ~14k→~7 calls for 151-org cohort), Slack one-call posting up to 250 accounts, 90-min turn budget both admin paths, 25-min lease fence on daily run; whale-safe day-slicing + 50k gate preserved; PostHog users descoped to PHO-13647

## 2026-07-13

- PHO-13589 merged (#11222, 0937221f): admin analytics catalog Snowflake->prod Postgres, 20 curated templates all <50k worst-case-org EXPLAIN (226-org audit), whale-safe 1-day window splits, 3 covering-index CONCURRENTLY migrations, EHR template port post-#11190, deny-tier definition-time validator, Snowflake admin tools deleted
- STAGING-MODAL-2 investigation: confirmed out-of-order workflow_run race deployed stale pre-fix voice code (staging-deploy checks out workflow_run.head_sha; slow older Main CI run fires deploy after newer); live healthy deploy confirmed post e013af5f; race fix committed unpushed at b4d5e7f9 on henry/staging-modal-deploy-investigation-2 (worker killed per Henry before PR)
- PHO-13559 merged (#11190, 47ca2204): get_org_ehr_state composite + EHR Snowflake catalog entries + ehr_sync_recent_errors redacted query; survived provider hang (interrupt->replace), 5 review MEDIUMs fixed incl. real-autoload isolated-DB route test
- **WIKI-99** — render archived headless runs from archived events.jsonl — after archive (store.py archive_current pops registry entry + deletes runtime run dir), /api/agents/{ticket}/session falls back to heuristic provider-transcript search (transcripts.find_session over ~/.codex/sessions + ~/.claude/projects) which archive never persists; if rollout files are cleaned/rotated or the WIKI-40/45 resolver heuristics miss (ticket regex / cwd slug / archived_at-as-spawned_at), rendering degrades to clean_pane_log over the archived .log — for headless runs that .log IS raw NDJSON, so the UI shows one giant unreadable JSON terminal blob. Archive ALREADY persists normalized events.jsonl (store.py:810-813) precisely for re-render but agent_session never reads it. Fix: in agent_session (main.py:1342-1448), when registry current is gone and _archive_hint finds an archive session dir, render from the newest archive session's events.jsonl through the same normalized-events payload path as live headless runs (format/version identical to live path, working=false, queue empty); keep provider-native find_session as first choice ONLY if it resolves (it can be richer, e.g. includes subagents), archived events.jsonl as second, clean_pane_log blob as last resort for pre-headless tmux archives. Cover /session/older pagination against archived events too. Regression: Playwright fixture — spawn fake run, archive it, assert structured events still render (not one terminal blob); unit test the fallback ordering. Empirical repro 2026-07-13: WIKI-98 post-archive renders only because the codex rollout still exists on disk and heuristics happened to hit. — cdx:WIKI-99 (gpt-5.6-sol, spawned 2026-07-13)
- STAGING-MODAL investigation complete: staging deploy-modal-voice red since 4fc65890 (secret_redaction.py eager import reads core/src/security/secret_redaction_patterns.json; Modal voice image never mounts it) — containers crash on import, rolling deploy pins old revision; fix = Image.add_local_file in services/voice/modal_app.py
- HOTFIX-DUP-TOOL merged (#11218): removed duplicate always-mounted post_account_health_threads_to_slack registration; skill/registry group now single surface; real-runtime uniqueness regression added
- WIKI-97 (owner cdx:WIKI-97): transcript markdown drops list/header block structure for LLM-style markdown — repro (Henry 2026-07-13 screenshot): assistant message with sections like `**Session & transcript**` immediately followed by `11. Time-travel forks — ...` rendered as one run-on paragraph (only the first section, whose list starts at `1.`, rendered as a real list). Root cause: renderer is react-markdown 10 (strict CommonMark, micromark) + remark-gfm + remark-breaks (frontend/src/markdown.tsx, used by session.tsx transcripts) — per CommonMark an ordered list can only INTERRUPT a paragraph when it starts with 1; lists starting at 11/17/23 after a bold header line with no blank line stay inside the paragraph, and remark-breaks turns the newlines into <br> so it 'renders well' but flat. Fix: add a preprocessing step (or small remark plugin) for TRANSCRIPT rendering only that lets ordered lists starting >1 open a list at a line start after a linebreak — e.g. insert a blank line before lines matching ^\d+\. when the previous line is non-empty non-list text; must NOT touch fenced code blocks/inline code, and must not reformat vault-note editor rendering (LLM-leniency is a transcript concern; notes should stay spec-strict). Add regression fixture with the exact failing shape (bold header line + list starting at 11) + keep ui-demo passing.
- WIKI-98 (owner cdx:WIKI-98): raw HTML in transcript markdown silently stripped — react-markdown 10 pipeline (frontend/src/markdown.tsx) has no rehype-raw, so raw HTML nodes from assistant output (<details>, <sub>, <br>, <kbd>) are dropped with no visible trace. Fix: TRANSCRIPT rendering only — surface raw HTML as visible escaped literal text (e.g. render as inline code), do NOT add rehype-raw (keeps XSS surface closed). Vault-note editor rendering untouched. Never alter fenced code blocks/inline code. Regression fixture: assistant message containing <details>, <br>, <sub> shapes. Coordinate: WIKI-97 adds transcript-only markdown preprocessing in the same surface — branch from main, expect rebase after WIKI-97 merges.
- PHO-13598 merged (#11216, 56e145ba): search_run_data timeout now covers line materialization; flaky wall-clock test deflaked
- WIKI-96 (owner cdx:WIKI-96): composer pending row stuck at 'sent · waiting for transcript' + same message double-rendered in queued list — follow-up to WIKI-93/#75 optimistic sends. Repro (Henry 2026-07-13): sent 'Test test test' to cc orchestrator 'wiki' mid-turn; message delivered fine but UI never reconciled. Root causes: (1) `reconcilePendingUserMessages` (frontend/src/transcript-store.ts:154-178) requires exact trimmed-text hash AND event ts within [firstSeenTs-2s, firstSeenTs+30s] (PENDING_RECONCILE_WINDOW_MS=30_000) — mid-turn sends get queued/replayed by supervisor so the transcript echo lands after the 30s window and the pending row never clears; (2) cc user echoes can include hook-injected text (UserPromptSubmit context) → hash mismatch; (3) deliverPending (frontend/src/session.tsx:3029-3052) keeps the pending row when backend status != 'queued' while the queue snapshot from result.messages can still contain the same text → both pending row AND queued row render. Fix direction: round-trip a durable pending_id through supervisor into the normalized user event and reconcile by id (WIKI-93 item 3 fallback upgrade); on queue-snapshot match, collapse to single representation; drop/expand the 30s window for known-queued sends.
- **WIKI-94** — session view O(N) cost per poll (buffering on 50M+ token windows) — three layered fixes shipped together, ordered by ROI. **(A) Stabilize events-array reference in `mergeSession` (`frontend/src/transcript-store.ts:190`)**: short-circuit and return `current` unchanged when `result.tail_from === base + events.length && result.patches.length === 0 && result.base === current.base` — no delta means no new object. Kills the memo-invalidation cascade downstream (session ref changes on every poll today even when nothing changed, forcing groupEvents + buildVirtualLayout to rerun on every poll). **(B) Incremental groupEvents + buildVirtualLayout (`frontend/src/session.tsx:1167, :1085`)**: both are prefix-independent — group[i] depends only on events[0..i], layout tops[i] depends only on heights[0..i-1]. Cache last (groups, heights, tops) alongside events; on delta, find first changed event index k and recompute only groups[k..] + tops[k..]. Turns O(N) per poll into O(delta). Wire the cache into the useMemo refs so it survives session ref changes (which #4 also cuts). **(C) Server tail window on initial load (`backend/app/transcripts.py:1742 full_reset`)**: instead of returning `deepcopy(events)` for the whole history at `cursor=0`, return the last N events (default 500 or ~200KB by-token-budget) + `has_older: true` + `base = total - N`. Add a `?older_before=<base>` path to page older on demand. 50M-token sessions almost never need the top of the transcript visible immediately. Also drop the defensive deepcopy on the full-reset path (the returned dict is read-only downstream) — biggest server-side latency win. **Verification:** benchmark timing on a 50k-event fixture: baseline switch + first-poll + steady-poll, then after each fix (A/B/C). Assert steady-poll drops to sub-10ms after (A), first-poll drops to sub-100ms after (B), initial-switch drops to sub-500ms after (C). Playwright regression: switching sessions with the fixture shows the transcript within 500ms, ISN'T jank while polling. **Non-goals:** do NOT touch mermaid/plot/vega rendering itself — under-threshold artifacts mounting all-at-once is a WIKI-95 follow-up (IntersectionObserver-gated renderers). Do NOT change the delta protocol wire shape beyond adding `has_older` + the `older_before` route.
- PHO-13570 merged — admin org context index follow-ups (Core org cross-check, Notion org scoping, Snowflake live-only semantics), PR #11201, squash f4fba68e
- PHO-13554 merged — admin agent email read access (Core email endpoints + admin tools), PR #11192, squash 4fc65890
- PHO-13556 admin org context index merged as PR #11193 (6b7bf0562e)
- [WIKI-93](https://github.com/hwang2409/wiki/pull/75) composer optimistic pending row — composer clears immediately + user-shaped pending row renders synchronously with "sending…" indicator; reconciled against real transcript events via text-hash + first_seen_ts within 30s window (provider-level pending_id round-trip deemed too invasive across both providers). Dropped mode guard on queue updates so `now`-mode auto-queue is visible. Failed sends preserve Retry + Edit affordances. Auto-queued vs explicit on-idle distinguished by tooltip. Follow-up nits: double-click retry sub-ms race, `status: string` should tighten to `"sent" | "queued"` union, 30s reconcile window edge cases.
- [WIKI-92](https://github.com/hwang2409/wiki/pull/74) artifact side-panel expansion — inline artifacts stay compact only above per-kind thresholds (table > 30 rows, code > 100 lines, image > 400px, mermaid > 20 nodes, svg > 400px, plot > 100KB); under threshold renders exactly as WIKI-85. Split-pane on right (55/45 default, drag-resize), fullscreen slideover on < 900px. Tabs with dedupe by artifact_id + LRU-10 recently-closed tray. Per-session state (sort/zoom/fold/pan) via transcript-store `PanelState`. URL deep-link `?panel=<ticket>&artifact=<uuid>&tab=<uuids>&focus=<uuid>` restores on reload. Keyboard: Cmd+Shift+A toggle, Cmd+Shift+[/] tab nav, Esc/Cmd+W close, Cmd+0 reset zoom, Cmd+F code find. Pin-to-vault menu stub disabled with "Coming soon".
- [WIKI-91](https://github.com/hwang2409/wiki/pull/73) migrate live runs across supervisor upgrades — snapshot active runs (control_attached OR provider_pid alive) BEFORE killing stale supervisor; interrupt working turns up to 10s + gracefully stop providers through old daemon; issue WIKI-38 recovery Replace calls against fresh daemon (session_id preserved, model/provider/effort defaulted from old RunRecord); orphan fallback via WIKI-77 process-identity check when old daemon can't clean up cleanly. v1 loses in-flight turn if drain times out (Continue/Defer UI follow-up). Follow-up nits: no concurrency lock on `ensure_running()`, silent RPC-failure swallow, Replace-failure surfacing depends on caller.
- **PHO-13558** — 6 new Snowflake catalog entries for per-org deployment signals — shift breakdown, outreach performance, coverage funnel, Modjo call quality, Chrome ext events, product-agent-run outcomes ([PR #11186](https://github.com/phoebe-health/phoebe/pull/11186))
- [WIKI-89](https://github.com/hwang2409/wiki/pull/72) wiki-artifacts MCP callability after app upgrades — root cause was a stale detached supervisor (PID 42586 from July 10) that survived the July 13 bundle upgrade; both providers' "not callable" / "not registered" symptoms came from the same pre-WIKI-85 daemon. Fix: `agent_runtime/version.py` fingerprints the running daemon's build; `client.py` detects mismatch, kills the stale process, spawns fresh. Adapter argv/env for both codex + claude asserted; new `test_wiki_artifacts_e2e.py` + `wiki-89-artifact-callability.playwright.mjs` spawn both providers through real supervisor and assert `tool_use.name === "render_artifact"` frames land in the transcript (the assertion missing from WIKI-85 that let this ship). Follow-up nits: version.py fingerprint compute lacks exists/readable guards; concurrent `ensure_running` can hit flock timeout.
- [WIKI-90](https://github.com/hwang2409/wiki/pull/71) Claude CLI shortname→API-ID mapping — versioned catalog shortnames (`opus-4.7`, `sonnet-4.6`, `haiku-4.5`) translated to full API IDs (`claude-opus-4-7`, `claude-sonnet-4-6`, `claude-haiku-4-5`) at the adapter boundary; unqualified aliases (`opus`, `sonnet`, `haiku`) and `claude-fable-5` pass through unchanged. Fixes WIKI-85-DEMO-CC blocked at `model_not_found 404`.
- [WIKI-85](https://github.com/hwang2409/wiki/pull/69) Wiki.app native artifact protocol — `render_artifact` MCP stdio server bundled into wiki-backend sidecar, wired into codex + claude adapter startup; 6 kinds (mermaid, svg, image, table, plot, code+diff) with 100KB text / 5MB image caps, sentinel-wrapped stdout; transcripts parser (not supervisor normalizer) specializes `tool_use.name === "render_artifact"` → `artifact` session event; backend `/api/agents/{ticket}/artifact/{uuid}` serves live+archived paths (archive-copy step preserves image bytes before `runs/<run-id>/` rmtree); frontend `<ArtifactBlock />` dispatches per kind with mermaid.js (theme observer), DOMPurify SVG, virtualized+sortable+exportable table, vega-embed plot, Shiki code+diff, shared Copy/Download/Inspect chrome. Plan-dispute cycle nailed both surface holes (archive rmtree + normalized-events-not-in-session-view) before shipping.
- **PHO-13509** — Deploy B — physical DROP COLUMN for args_hash + estimated_cost_usd + cost_alert_50_percent_fired (post-PHO-13490) ([PR #11173](https://github.com/phoebe-health/phoebe/pull/11173))
- [WIKI-86](https://github.com/hwang2409/wiki/pull/68) render `AskUserQuestion` `multiSelect: true` as checkbox list — transcripts normalizer carries per-question `multi_select` metadata; session view renders checkboxes for multi + radios for single (mixed cards supported); submit gate requires ≥1 checked; adapter converts array answer to Claude Code's `updatedInput.answers` comma-separated string contract. Preserves single-select auto-submit + "Other" custom-input path.
- [WIKI-87](https://github.com/hwang2409/wiki/pull/67) inline code fence overflow / no-wrap cutoff — scoped the wide-code breakout rule to `.markdown-reading-view .markdown-preview-view pre` (was global) so session code fences stay pane-width and expose shiki's existing horizontal scrollbar; tool-output surfaces get `white-space: pre-wrap; overflow-wrap: anywhere` to wrap long tokens. Playwright covers 200-char JSON in fence + 200-char tool line.
- [WIKI-78](https://github.com/hwang2409/wiki/pull/66) /agents UI dead-run affordance — detect (state=completed OR control_attached=false+provider_pid=null OR state_reason=adapter_lost), badge with "adapter detached — archive to reset" chip using existing `--accent-warning`, one-click Archive button (no modal), inline error strip on failure. Applies to both orchestrators + workers. Playwright covers all 3 detection paths.
- **PHO-13490** — rip out ALL admin tool-call budgets — no per-run caps, no per-tool caps, no rate limits, no cost meter, no loop-detect (kept audit + tool_call_id idempotency + statement_timeout) ([PR #11159](https://github.com/phoebe-health/phoebe/pull/11159))
- [WIKI-84](https://github.com/hwang2409/wiki/pull/65) monospace size slider + cross-pool font pickers — added `--font-monospace-size` (default 13.5px, stored `wiki-font-size-mono`) with third slider row + reset. Merged MONO/UI/TEXT font lists into `ALL_FONTS` (dedupe by label) so each picker shows every available font. New mono-size var binds via one low-specificity `:where(...)` rule against major mono surfaces.
- [WIKI-83](https://github.com/hwang2409/wiki/pull/63) bundle Consolas for Powerline TTFs — WIKI-82's `local()` @font-face rules stayed invisible: WebKit's Font Fingerprinting Protection (macOS Ventura+ / WebKit 15.4+) restricts `local()` to a ~50-font system list, even inside @font-face. Copied 4 TTF weights into `frontend/public/fonts/` and switched src to `url()` — explicit URLs bypass the restriction.
- [WIKI-82](https://github.com/hwang2409/wiki/pull/62) register `@font-face` for Consolas for Powerline — WIKI-58 added picker option but WebKit on macOS Sonoma+ blocks bare family-name lookups for `~/Library/Fonts/` (anti-fingerprint gate). Fix: four @font-face rules with `src: local(...)` aliases for regular/bold/italic/bold-italic. System-path fonts (Menlo, Consolas plain) escape the gate and need no workaround.
- **PHO-13477** — align Snowflake tool with provisioned account — PHOEBE_ADMIN_MCP_ROLE + PHOEBE_WH + provider_error_snippet on 4xx ([PR #11136](https://github.com/phoebe-health/phoebe/pull/11136))
- **PHO-13413** — admin tool cost controls — loop-detect + $ cost meter + downstream rate limits ([PR #11074](https://github.com/phoebe-health/phoebe/pull/11074))

## 2026-07-12

- **PHO-13396** — admin agent tool retry storm — statement_timeout + tool_call_id idempotency + interrupt cancellation ([PR #11072](https://github.com/phoebe-health/phoebe/pull/11072))
- **PHO-13417** — run_admin_snowflake_query overhaul — catalog, artifact overflow, warehouse/role, cost meter, typed params ([PR #11099](https://github.com/phoebe-health/phoebe/pull/11099))
- **PHO-13415** — Kratos identity sync crash on multiple pending invitations ([PR #11073](https://github.com/phoebe-health/phoebe/pull/11073))

## 2026-07-10
- **tools** — cleared destructive_command_guard PreToolUse hook from /Users/henry/.claude/settings.json
- [WIKI-81](https://github.com/hwang2409/wiki/pull/61) cross-provider Replace — Replace endpoint accepts optional `{kind, model, effort}`, dispatches to correct provider (codex/claude) on kill+respawn. Shared Replace modal (kind toggle, kind-filtered model dropdown, conditional effort) on both fleet + session surfaces. `model_changed` lifecycle marker. Real-process orchestrator swap cc→cdx→cc verified. Follow-up nits: fake codex adapter effort asymmetry (fake.py:119), replace-modal.tsx:1128 effort default not kind-aware.
- **tools** — removed destructive_command_guard Codex hook and /Users/henry/.local/bin/dcg
- **PHO-13390** — expose post_account_health_threads_to_slack on admin agent + daily 07:00 cron ([PR #11063](https://github.com/phoebe-health/phoebe/pull/11063))
- [WIKI-64](https://github.com/hwang2409/wiki/pull/50) GitHub URL preview cards — `/api/gh/preview` backend proxy with gh-CLI-backed PR/issue/commit lookups, LRU-128 cache, ETag/304, 5min TTL; frontend renders inline cards for github.com URLs in assistant + tool text with per-URL Promise dedupe. Two adversarial-review passes required (ANSI+URL mutex bug — fixed via text/URL split with `renderAnsi()` on text segments + cards as pre-siblings; unbounded cache → LRU-128).
- [WIKI-56](https://github.com/hwang2409/wiki/pull/60) queue-till-idle model change — new `POST/DELETE /api/agents/{ticket}/set-model` + supervisor RPC persistence; applies at idle/new-turn boundary via replacement-session replay; session-footer becomes clickable model picker with `queued: <new>` state + cancel affordance + inline `model changed` marker.
- [WIKI-80](https://github.com/hwang2409/wiki/pull/59) allow Codex models as orchestrators — `SpawnOrchestratorIn` gains `kind` + `effort`; `spawn_orchestrator` picks provider ("codex" if kind=cdx else "claude") + threads effort through; frontend modal shows kind toggle, model dropdown filtered by kind, effort selector conditional on kind=cdx. Depends on WIKI-79 catalog.
- [claude-fable-5](https://github.com/hwang2409/wiki/pull/58) added Claude Fable 5 to cc model catalog at top of block; test allowlist strings + list_models coverage updated.
- [WIKI-79](https://github.com/hwang2409/wiki/pull/56) Codex 5.6 catalog fix — replaced invalid bare `gpt-5.6` MODEL_OPTIONS entry with the three named variants `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`. Regression coverage: accept `gpt-5.6-sol` at spawn + reject bare `gpt-5.6` via WIKI-75 allowlist. WIKI-55 session-model-catalog picks up changes via `list_model_options` — no frontend edit needed.
- [native-build fix](https://github.com/hwang2409/wiki/pull/57) `scripts/build-native-backend.sh` now runs `npm install` before `npm run build` so stale checkouts install shiki (WIKI-59 dep) before tsc runs.
- [WIKI-59 hotfix](https://github.com/hwang2409/wiki/pull/55) shiki.tsx TS2345 — TS 5.9 didn't narrow `let pending` after reassignment inside `if (!pending)`; refactored to const-bound `pending: Promise<void>` after awaiting any existing in-flight promise.
- [WIKI-74](https://github.com/hwang2409/wiki/pull/51) scope session timestamps to user + last-assistant-of-turn — `computeTimestampKeys(groups)` walks groups once, tags user messages + each assistant whose next message-kind group is a user turn or EOF. `VirtualSessionRow` gets `showTimestamp` prop; memo bails on change. Cuts noise from WIKI-72 to two anchors per turn.
- [WIKI-77](https://github.com/hwang2409/wiki/pull/54) archive endpoint orphan-PID tolerance — detects PPID=1 orphan providers, kills the process group (not just leaf PID) with `psutil.Process.create_time()`-pinned identity check to defeat PID recycling, permits archive to proceed. Two adversarial-review passes (identity-check missing + lstart locale/precision weakness → psutil swap). Rebased onto WIKI-76.
- [WIKI-76](https://github.com/hwang2409/wiki/pull/53) supervisor reaper for adapterless runs — monotonic detach-time bookkeeping + per-run-lock reaper transition to `state=completed, state_reason="adapter_lost"` after `WIKI_REAPER_GRACE_SECONDS`; archive path clears `detached_at` bookkeeping to prevent map leak; env-backed interval + grace + active-quiesce exempt. Two adversarial-review passes required (wall-clock timer bug + archive-path leak).
- [WIKI-75](https://github.com/hwang2409/wiki/pull/52) spawn model-ID validation — `_require_allowed_model` gate at both `spawn_agent` and `spawn_orchestrator` returns 400 with allowed-values list before touching the supervisor; fixes `opus-4.8` typo crashing adapter (`RunRecord` stuck dead). Also corrects the shared model catalog (adds `opus-4.7`, `sonnet-4.6`, `haiku`, `haiku-4.5`).
- WIKI-46 already-merged on origin/main (`_session_paths.pop(ticket, None)` at `backend/app/main.py:2281` inside `_changed_registry_tickets`). Spawned cdx worker verified regression + smoke; no PR needed. Todo entry was stale.
- [WIKI-59](https://github.com/hwang2409/wiki/pull/49) Shiki syntax highlighting for markdown code fences + Bash tool rows (replaces rehype-highlight; 15 langs, 12 themes, lazy-loaded, MutationObserver on data-theme). Follow-ups: highlighter promise error recovery, orphan .hljs-* CSS pruning, per-mount MutationObserver → app-level singleton, Playwright token assertion (not just container)
- [WIKI-73](https://github.com/hwang2409/wiki/pull/48) revert WIKI-58 nit 1 — thinking events back inside ActivityGroup with empty-text guard (opus 4.7 encrypted thinking = empty CLI stream)
- [WIKI-72](https://github.com/hwang2409/wiki/pull/47) event timestamp hover — relative labels + native-title absolute tooltip on session event rows; shared 30s clock via useSyncExternalStore, tabular-nums, 9 unit tests

- **wiki** — [WIKI-71](https://github.com/hwang2409/wiki/pull/46) ANSI colors merged (#46, 69dd20d) — in-repo SGR parser (~110 LOC) renders 16-color palette across mono/gruvbox/solarized/dracula/nord themes via light-dark(); wired to bash stdout/stderr + tool output + pane-log; 14 unit tests; no npm dep
- **WIKI-71** — ANSI color rendering for terminal-style outputs — Bash tool_result / codex commandExecution outputs contain ANSI escape codes today rendered as raw \x1b[... noise. Parse via ansi-to-html or a small in-repo parser, render color/bold/underline. Preserve line endings + monospace. Extend terminal-runtime.ts helpers.
- **WIKI-73** — WIKI-58 thinking rows render empty — Claude opus 4.7 (verified 2026-07-10 run 5dfbf71f) streams 186 thinking content_blocks + thinking_delta events but .thinking text is always empty; only encrypted signatures present. Extended-thinking on opus 4.7 encrypts server-side; plaintext not exposed to CLI stream. WIKI-58 (#45) promoted thinking to top-level inline rows → they render as empty divs. Fix option B: hide empty thinking rows (event.text.trim() === '') at the render / height-estimator gate. Fix option C: show 'encrypted thinking' chip placeholder w/ small signature-preview badge. Recommend B — quieter, matches reality. Backends where thinking IS populated (older Claude, some codex reasoning summaries) still show inline correctly. Also verify heights estimator returns 0 for hidden rows so no phantom space.
- **wiki** — [WIKI-58](https://github.com/hwang2409/wiki/pull/45) inline thinking + Consolas for Powerline merged (#45, 467c1a2) — thinking events promoted to top-level virtualized rows w/ height estimator case; ActivityGroup now tool-only; MONO_FONTS gains Consolas for Powerline family for macOS installs
- **wiki** — [WIKI-57](https://github.com/hwang2409/wiki/pull/44) click race hotfix merged (#44, 78a263e) — queue AskUserQuestion answer by tool_use_id when respond() arrives before Claude's control_request; drain on control_request; TTL on turn end + close; adapter tests cover reversed-order race + happy path + overwrite + cleanup
- **WIKI-58** — nit-pair — (1) render Claude/Codex 'thinking' events INLINE as top-level rows instead of bundling them into the collapsed activity group with tool calls (session.tsx:900 groups tool+thinking together → .session-thinking rendered inside ActivityGroup at session.tsx:1247). Fix: move thinking events to first-class row like user/assistant/tool are. Keep .session-thinking styling for the row. Preserve encrypted-flag chip. (2) 'Consolas for Powerline' font invisible in picker because MONO_FONTS lists 'Consolas' (family='Consolas') and isFontInstalled canvas-metrics check at settings.tsx:302 does exact-family measurement — the local 'Consolas for Powerline' family name never matches. Add new MONO_FONTS entry {label:'Consolas for Powerline', family:'Consolas for Powerline', stack: '"Consolas for Powerline", ' + MONO_TAIL} at settings.tsx:170 area. Preserve existing 'Consolas' entry for Windows users. Both nits are ~10 line surgical touches on frontend/src/session.tsx + frontend/src/settings.tsx. Screenshot in PR body: (a) thinking events showing inline in transcript (b) Consolas for Powerline in picker with preview rendered in that font.
- **WIKI-57** — fix WIKI-54 click race — pending AskUserQuestion overlay renders on stream_event.content_block_start (~2s before Claude CLI's own control_request subtype=can_use_tool with the answer request_id lands). If Henry clicks in that window, adapter respond() raises 'unknown, canceled, or stale Claude server request id: <tool_use_id>' (claude.py:740). Repro 2026-07-10: run 5dfbf71f, seq 3514 content_block_start@18:19:29, seq 3524 control_request@18:19:31, 2.45s gap. Fix (option C, queue-till-ready): in claude.py adapter, add self._pending_answers_by_tool_use_id: dict[str, dict] map. respond() called with tool_use_id that has no _server_request_ids entry → do NOT raise; store the answer keyed by tool_use_id, return 202-ish success shape. When control_request (subtype=can_use_tool) with matching tool_use_id lands (grep for where _server_request_ids gets populated), drain the queued answer immediately (send control_response with updatedInput.answers). Idempotency: cap the queue at 1 answer per tool_use_id — later click replaces earlier. TTL: drop queued answer if the tool_use_id disappears (turn ends, session dies). Frontend NO changes; optimistic UI already handles latency. Test: unit test where respond arrives before control_request; assert answer is delivered when control lands. Also test the current-happy-path (control first, respond second) still works. Bumps: light touch on main.py + claude.py; potential merge conflict with in-flight WIKI-55 (session footer) — keep patches small on claude.py, no main.py session-endpoint changes. — owner: cdx:WIKI-57
- **WIKI-55** — model badge in session footer + fresh model list — backend agent_session (main.py:892) returns model + kind + provider from registry current in payload alongside existing format/path/tokens/dispositions. Frontend SessionData type: add model?: string. Footer render (session.tsx:1767): {format} · {model} · {tokens} · Unknown {N} — drop path basename. Frontend model dropdown (spawn-orchestrator + spawn-worker forms) reads from single shared model list grouped by provider; add opus-4.8 + gpt-5.6 (released 2026-07-09). Locate current model source of truth (grep for gpt-5.4, opus, claude-opus in backend + frontend + supervisor to find canonical enum) and centralize if scattered. Enum-validate at spawn dispatch (composes with WIKI-48 RPC allowlist work). No mid-run change in this ticket — WIKI-56 covers that.
- **wiki** — [WIKI-55](https://github.com/hwang2409/wiki/pull/43) model badge in session footer + centralized model catalog merged (#43, b3adc7a) — footer now shows {format} · {model} · {tokens} · Unknown {N} across all 4 session shapes; single agent_models.py catalog exposed via GET /api/models; spawn-orchestrator + spawn-worker dropdowns read from it; opus-4.8 + gpt-5.6 added; SpawnIn/SpawnOrchestratorIn enum-validated; drive-by claude.py sleep(0) event-loop yield + accounts watchdog re-add flagged for WIKI-59 nit cleanup
- PHO-13373 merged: extended account-health Slack thread detail block with typed metric sections (outreach, SMS/calls, engagement, users, clock, phase) matching legacy flat-post parity. Schema extension backward-compatible (new fields default None); PHO-13369 idempotency + URL escape untouched. 2 files / 1334 lines. PR #11040.
- **wiki** — [WIKI-54](https://github.com/hwang2409/wiki/pull/42) pending AskUserQuestion overlay + click-to-answer merged (#42, 16fbb9c) — real-time headless-supervisor pending question card via raw.jsonl tail; QuestionRow onClick posts to /respond with control_response.updatedInput.answers shape Claude accepts; tool_use_id plumbed through session events; 14 files, additive under _is_headless
- **WIKI-54** — pending AskUserQuestion invisible under supervisor era — Claude Code TUI batches AskUserQuestion tool_use + tool_result writes to on-disk jsonl until user answers (verified 2026-07-10 55s pending window, .codex/WIKI-42-rendering-gap-audit.md:40-53). agent_session (backend/app/main.py:892) reads on-disk transcript → pending question invisible in wiki app; Henry hit it 2026-07-10 replacing session d2330671. Fix: under headless mode, overlay pending question events from supervisor raw.jsonl (WIKI_AGENT_RUNTIME_DIR/runs/<run-id>/raw.jsonl). Tail raw stream, accumulate stream_event.content_block_start tool_use blocks where name=='AskUserQuestion' with subsequent input_json_delta partials, emit as pending kind='question' event tagged with tool_use_id; drop when tool_result for that id lands (or native jsonl catches up). Extend transcripts.py _emit_question_events call path OR add new headless-overlay helper called from _session_delta_payload before returning. Verify by scripted spawn → orchestrator issues AskUserQuestion → frontend renders card within 2s (not 55s+). Test fixture: raw.jsonl slice with 3-way question, no matching tool_result yet.
- HOTFIX-CHANNEL merged: wired _ACCOUNT_HEALTH_SLACK_CHANNEL_ID from TODO-HENRY to C09CLCZTWG7 (activates PHO-13274 account-health Slack thread automation). PR #11035, commit bf248201.
- PHO-13369 merged (PR #10968 follow-up): account-health Slack thread idempotency hardened against subset-varying retries (option b: loader filters by run_id, dead aggregate write dropped), _markdown_link + _markdown_link_replacement escape | and >, _ACCOUNT_HEALTH_ADMIN_CALL_URL_PREFIX deleted; regression tests for subset-retry + link-escape. PR #11028, commit 68bfaa84.
- WIKI-53 (#41) supervisor Claude workers run bypassPermissions — parity with codex approvalPolicy never; stdio prompt-tool stays for policy-forced prompts
- WIKI-52 (#40) macOS /tmp symlink vs supervisor store guards — RuntimePaths resolves parent chain, final component stays unresolved; spawn unblocked on macOS
- WIKI-51 (#39) frozen sidecar --supervisor routing — native_server.py never learned the flag WIKI-42's client re-execs with; supervisor never started in Wiki.app. Also stale-test note: 2 backend tests broken on main post-#35 (queue shape + removed message_dispatcher)
- WIKI-42 (#35) HEADLESS RUNTIME MIGRATION — tmux killed as agent runtime. Detached wiki-supervisor daemon on 0600 Unix socket owns provider CLIs (codex app-server JSON-RPC, claude stream-json). Adapter surface: start/resume/send_now/send_on_idle/interrupt/stop/replace/status/events/archive/respond. Raw NDJSON + normalized events per run. Restart-survivability + mixed-fleet gate proven. Watchdog dead, accounts rotation via provider events. 15059+/410- across 40 files.
- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274): compare_agent_runs trace comparison (audit-validated P1) — cdx:PHO-11274 worker
- WIKI-47 (#38) disposition footer polish — collapse R/S/I/U to just Unknown N in bottom-right chip
- [TASK] REVIEW-10983: adversarial review of PR #10983 (call analysis approval + failure alerts) — cdx:REVIEW-10983 worker

## 2026-07-09

- **phoebe** — HOTFIX-CATALOG-2 regenerate admin schema catalog after PHO-13242 `shifts.pay_rate` merge ([#10978](https://github.com/phoebe-health/phoebe/pull/10978) merged c7ec74e7c9)
- [URGENT] HOTFIX-CATALOG: main red — regenerate admin schema catalog post PHO-13273 merge — cdx:HOTFIX-CATALOG worker
- WIKI-41 (#37) native transcript surfaces — normalization for AskUserQuestion / Monitor / progress / system-subtype / tool_reference / image / custom-title / permission-mode / Codex encrypted_content; disposition inspector (rendered/summarized/ignored/unknown counts); Playwright coverage; closes WIKI-42 step-5 rendering acceptance
- [P?] [PHO-13273](https://linear.app/phoebework/issue/PHO-13273): rebase-onto-main (schema-wide admin DB reads via generated catalog) — cdx:PHO-13273 worker
- phoebe-mastermind cross-orch bug#3 (cwd-not-worktree case, non-numeric-suffix ticket VA-WHEATRIDGE) — already covered by WIKI-45 (#34, c8c1da4) since session_id-first glob is independent of both slug shape and kickoff-ticket regex. No new work; symlink workaround unnecessary once phoebe wiki rebuilds.
- [TASK] VA Wheatridge availability report v2 (customer-friendly, includes 117 with unavailability blocks) — cc:VA-WHEATRIDGE worker
- [TASK] VA Wheatridge caregiver availability report — cc:VA-WHEATRIDGE worker
- [PHO-13278](https://linear.app/phoebework/issue/PHO-13278): truncation policy + artifact UI — cdx:PHO-13278 worker
- WIKI-46 (#36) _session_paths cache invalidation on handoff — pop entries for tickets in _changed_registry_tickets diff; unblocks every future cc↔cdx handoff
- [PHO-13307](https://linear.app/phoebework/issue/PHO-13307): harden Slack projection + MessageBubble artifact-only rendering (follow-up findings from PHO-13277) — cdx:PHO-13307 worker
- [PHO-13304](https://linear.app/phoebework/issue/PHO-13304): tighten delayed-reply survey attribution (follow-up findings from PHO-13157) — cdx:PHO-13304 worker
- [PHO-13277](https://linear.app/phoebework/issue/PHO-13277): admin agent charts as messages + no point limits
- WIKI-45 (#34) cc-path resolver session_id-first — mirror cdx pattern; unblocks phoebe UI mapping for PR-10475 and any cc worker whose ticket slug does not match worktree dir
- [PHO-13157](https://linear.app/phoebework/issue/PHO-13157): survey delayed-reply capture + chat-history link (folds 13106+13155, 2 customer reports) — cdx:PHO-13157 worker (gpt-5.4 xhigh)
- WIKI-38 (#32) agent Replace protocol: POST /api/agents/{id}/replace for worker+orch, env overrides for registry/status/tmp/archive/msg-queue, /agents Replace controls
- WIKI-39 (#31) watchdog revival mapping: live-registry recheck skips deregistered/terminal/re-windowed, refuse $HOME cwd, agent update --window --session after revive, bare same-day reset parse
- WIKI-44 (#33) subagent session events hotfix — 1-word revert to "claude-sub" fmt on backend/app/main.py:857, regression from #28; new regression test test_subagent_session_renders_sidechain_events
- [PHO-11274](https://linear.app/phoebework/issue/PHO-11274): compare_agent_runs trace comparison (audit-validated P1) — cdx:PHO-11274 worker
- **phoebe** — PHO-13280 admin tool semantics + discoverability merged (#10926 — recordings partition, compare models, source-health routing, collision validator)
- **WIKI-37** — terminal panes — xterm.js WebGL + pty WS + token auth (owner: cdx:WIKI-37)
- **phoebe** — PHO-13279 admin trace UI quality — real tool names, purpose titles, python highlighting merged (#10929)
- **phoebe** — PHO-13281 admin trace env-attributed DSN errors + tool constraint context merged (#10925)
- **WIKI-24** — window-switch state preservation merged (PR #29) — drafts/panels/scroll survive window switches
- **WIKI-32** — polling/delta overhaul — central transcript cache, pause hidden panes, patch-by-id deltas (subsumes WIKI-18) — spawn AFTER WIKI-31 merges
- **phoebe** — PHO-13216 Core dossier + funnel snapshot data products (#10856 — account limit cap, stripeErroredCount, typed tags, spill markers)
- **phoebe** — PHO-13227 subagent approval inheritance hole closed (#10861 — parent snapshot inherit + intersection + structural unmount, fail-closed hardening)
- **WIKI-31** — transcript virtualization — <6k DOM nodes, WebKit scroll p95 <16ms (owner: cdx:WIKI-31)
- **phoebe** — PHO-13251 cross-org feature-adoption survey tool merged (#10887, admin agent; count/list modes, jsonb-typeof guard, missing-row default coalescing)

## 2026-07-08

- **WIKI-33** — cold paths — async tokens cache, sidecar onedir eval, graph RAF pause (owner: cdx:WIKI-33)
- **WIKI-28** — external links open in default browser (web target=_blank audit + tauri opener/on_navigation) (owner: cdx:WIKI-28)
- **WIKI-30** — performance audit — why is the app sluggish; measured bottlenecks + ranked fix plan (owner: cdx:WIKI-30, investigation)
- **WIKI-29** — C-a leader must override composer insert mode — prefix chords (C-a 9 etc.) intercept at capture phase regardless of vim/chatbox state, like native tmux
- **WIKI-26** — auto-kill tmux window when an agent run is archived (wiki agent done / archive path kills window_id)
- **phoebe** — PHO-11274 compare_agent_runs merged (#10850) — facts-only run comparison w/ caveats, cold-tier trace pack; audit-validated P1
- **WIKI-23** — pane/window open semantics — (1) subagent inspect panel splits the OWNING PANE not the window; (2) clicking notes/docs/activity/graph opens a NEW tmux window by default (drag-in still embeds), never replaces focused pane
- **phoebe** — PHO-13225 call-recordings tool split merged (#10855) — index/read/search verbs replace 48%-success mega-tool; tag-write honestly approval-gated
- [PHO-13225](https://linear.app/phoebework/issue/PHO-13225): call-recordings tool split (audit P0 #1) — cdx:PHO-13225 worker
- **phoebe** — PHO-13218 tiered tool mounting merged (#10845) — 35-tool hot set + domain packs via load_skill + routing evals; ~20k tokens/run saved
- [PHO-13218](https://linear.app/phoebework/issue/PHO-13218): tiered tool mounting (hot set + domain packs + routing evals) — cdx:PHO-13218 worker
- **WIKI-25** — unify focused/unfocused pane styling — minimal frame indicator only (owner: cc:WIKI-25)
- **phoebe** — PHO-13231 search_run_data flood regression test merged (#10848)
- **WIKI-22** — pane focus remount bug — stable pane identity, focus as prop (owner: cdx:WIKI-22)
- **WIKI-21** — /tokens page — usage over time, cli/model filters, cached-split, incremental scan cache (owner: cc:WIKI-21)
- **phoebe** — PHO-13231 search_run_data flood regression test merge-ready ([#10848](https://github.com/phoebe-health/phoebe/pull/10848))
- **WIKI-20** — settings pickers for interface + note fonts (probe-gated previews, ui-state synced) (owner: cc:WIKI-20)
- **phoebe** — PHO-13226 authz design delivered — tiers/approvals/revocation/subagent design; decision: T2-only now (PHO-13227), T1/T3/T4 parked with triggers
- **WIKI-19** — watchdog revival v1.1 — (a) resume by EXPLICIT session id only, id resolved from richest-lineage rollout (kickoff-or-cwd match, largest/newest), launch cwd = worktree + auto-answer codex cwd-mismatch dialog (choose current dir); (b) preserve original tmux session (capture #{session_name} before kill); (c) detect 'access token could not be refreshed' pane signature as auth-dead -> kill+resume; (d) registry tracks current session id per worker (wiki agent update --session) and transcript resolver prefers it over discovery; (e) re-poke includes ticket id + status-file path
- **phoebe** — PHO-13215 Core data products T1 merged (#10830) — resolve_core_account_owner + get_core_account_book + Core endpoints w/ sanitized errors; Jake golden case at ≤3 calls
- [PHO-13215](https://linear.app/phoebework/issue/PHO-13215): Core data products T1 (resolver+book+endpoints) — cdx:PHO-13215 worker; T2=PHO-13216 queued; PR #10830 review/CI babysitting
- **WIKI-16** — pane-resize perf — transient CSS-var drag, commit on pointerup (owner: cc:WIKI-16)
- **WIKI-17** — native app persistence — stable port + server-side ui-state mirror (owner: cc:WIKI-17)
- **WIKI-14** — render Claude task lists in transcript (counts header + per-task status glyphs like TUI); prereq = JSONL event-coverage audit (unhandled codex/claude event types)
- **WIKI-12** — tmux window model redesign — cdx:WIKI-12
- **WIKI-15** — backend usage-limit watchdog — autonomous codex account rotation + fleet revival (owner: cc:WIKI-15)
- **WIKI-13** — agent-UI nits — composer growth must push transcript up (always readable above chatbox); font dropdown renders each option in its own font (preview); add Monaco, Consolas + all installed coding fonts to font list
- **wiki** — WIKI-10 Conductor-inspired restyle merged (PR #10) — transcript/composer/spawn-modal modernization, styling-only, 12-theme safe
- **wiki** — PR #9 unified session surface (pane parity, review side panel, compact checks) + PR #8 keyboard focus model (pane scope, vim layering, skill-picker keys) merged
- **wiki** — WIKI-9 merge-ready ([#9](https://github.com/hwang2409/wiki/pull/9)) — unified split/full agent session surface, review side panel, compact checks rows
- **phoebe** — PHO-13144 WellSky clock-in writeback fix merged (#10812) — QA action executor now honors live-call allowed_action_families for shift_clock_writeback; prod investigation via orchestrator tunnel; follow-up PHO-13207
- **wiki** — polish fleet complete: PR #5 twelve themes, PR #6 orchestrator spawn-from-UI, PR #7 tmux C-a leader keys + status bar + split-pane subagent fix — six PRs merged in one day via codex fleet
- **wiki** — WIKI-7 merge-ready ([#7](https://github.com/hwang2409/wiki/pull/7)) — tmux-style `Ctrl+A` fleet leader, bottom status strip, pane focus/zoom/close chords, split-pane inline subagent inspect
- **phoebe** — PHO-13206 admin tool audit delivered — 112 tools, 44 used/7d, 73.1% success; P0s: call-recordings split + Core account owner filtering; backlog re-verdicts on ticket
- [PHO-13206](https://linear.app/phoebework/issue/PHO-13206): admin agent full tool audit (usage/failure/scoping/gaps) — cdx:PHO-13206 worker (audit-only)
- **wiki** — polish fleet day 1: PR #2 review surface (gate worker PRs in-app), PR #3 transcript/CSS polish, PR #4 spawn-from-UI — all reviewed+merged
- **phoebe** — PHO-13074 tool error-taxonomy hardening merged (#10810) — uncategorized_internal structural fallback + silent-down Datadog alerting; inspect_codebase_wiki failure was pre-fixed by #10288, observability gap closed
- [PHO-13074](https://linear.app/phoebework/issue/PHO-13074): inspect_codebase_wiki silently down (58/59 prod failures, no error_category) — cdx:PHO-13074 worker
- **phoebe** — PHO-13144 RCA/fix for voice QA clock writeback action-family gate ([#10812](https://github.com/phoebe-health/phoebe/pull/10812) merge-ready)
- **wiki** — native macOS app (Tauri 2 + PyInstaller sidecar) merged, PR #1: plan→implement→review via codex workers WIKI-1/WIKI-2, 1:1 parity vs web app, web flow unregressed

## 2026-07-07

- **phoebe** — PHO-13172 Twilio delivery error visibility merged (#10769) — provider error codes now logged + persisted on contact attempts; investigation confirmed original incident was RingCentral 1000-char (fixed by #10466, no recurrence)
- **phoebe** — PHO-13153 admin run-data read/slice/diff tools merged (#10740) — completes read/search/slice/diff/snippet verb family
- [PHO-13153](https://linear.app/phoebework/issue/PHO-13153): run-data verb set (read/slice/diff) — cdx:PHO-13153 worker (stacked on 13146 branch)
- **phoebe** — PHO-13146 admin agent python snippets v0 merged (#10734) — run_admin_python_snippet subprocess sandbox + search_run_data with ReDoS hardening
- [PHO-13146](https://linear.app/phoebework/issue/PHO-13146): python snippets v0 (run_admin_python_snippet) — cdx:PHO-13146 worker (gpt-5.5 xhigh)
- **phoebe** — PHO-13165 Slack email-resolution hardening merged (#10754) — lower(email) functional index + case-duplicate ambiguity guard
- **phoebe** — PHO-13160 Slack account-linking fixes merged (#10749) — NULL-mapping 10min retry TTL, case-insensitive email match, /phoebe link copy; prod investigation documented on ticket
- [PHO-13160](https://linear.app/phoebework/issue/PHO-13160): Slack link failure Debbie Goble / phoebe-whole-life — cdx:PHO-13160 worker (investigation-first)
- **phoebe** — PHO-13142 admin chart artifacts merged (#10722) — admin.chart@1 artifact type, client-side declarative rendering, date-only temporal support
- [PHO-13142](https://linear.app/phoebework/issue/PHO-13142): chart artifact type (admin.chart@1, client-side declarative rendering) — cdx:PHO-13142
- **admin-agent** — PHO-13141 simple-ops speed: non-churned cohort one-call (SQL predicate + org-id bridge), pinned outreach recipe, <=3-call golden eval, account-lane descriptions de-collided ([#10720](https://github.com/phoebe-health/phoebe/pull/10720) merged 4b6e513d6f)
- **admin-agent** — PHO-13093 Phase 1: admin agent extracted into phoebe_admin_agent package (202 files, one-way dependency seam, zero behavior change) ([#10690](https://github.com/phoebe-health/phoebe/pull/10690) merged 12fa6c0c77)
- **admin-agent** — PHO-13133 /admin/agent polish: tool outputs collapse by default, composer outline removed ([#10689](https://github.com/phoebe-health/phoebe/pull/10689) merged 96bad98a6a)
- **admin-agent** — PHO-13133 /admin/agent polish: tool-output payload cards collapse by default, inner composer outline removed, screenshots committed, CI green + approved ([#10689](https://github.com/phoebe-health/phoebe/pull/10689))

## 2026-07-06

- **admin-agent** — PHO-13096 full /admin/agent frontend rewrite: Ramp-style retheme, artifact registry decomposition (6.9k-line monolith dissolved), transcript/inspection rebuild, real-E2E-verified, 6 worker sessions via handoff protocol ([#10634](https://github.com/phoebe-health/phoebe/pull/10634) merged 5bdf0c4b76)
- **admin-agent** — PHO-13111 PR review pipeline: bounded review + scoped GitHub write-back lane, head-SHA discipline, audit rows, allowlist ([#10651](https://github.com/phoebe-health/phoebe/pull/10651) merged 273dc70a44) — 12306 leg C done
- **admin-agent** — PHO-12634 MCP-to-native cleanup: MCP fallbacks removed from native clients, 30 stale env keys dropped, prototype MCP prod-impossible ([#10652](https://github.com/phoebe-health/phoebe/pull/10652) merged ab4c2b4177)
- **admin-agent** — PHO-13104 code-sandbox hardening: compound-word secret guard, symlink-escape check, denylist dedup ([#10640](https://github.com/phoebe-health/phoebe/pull/10640) merged 3edd8a0fe6)
- [PHO-13095](https://linear.app/phoebework/issue/PHO-13095): skill-load card display-title casing — cdx:PHO-13095 worker (queue overridden; aim to merge before tonight’s 13093 extraction)
- **admin-agent** — PHO-13073 code-sandbox tool surface PR #10608 merge-ready after rebase/review fixes; CI/Bugbot green and approved ([#10608](https://github.com/phoebe-health/phoebe/pull/10608))
- **cleanup** — cancelled 11 implemented/stale Internal Admin Agent tickets with evidence comments (PHO-11231, 11268, 11273, 11461, 11462, 11464, 11465, 11466, 11540, 11727, 12424)
- **admin-agent** — PHO-12937 type-aware tool-output renderers — diff/table/code/JSON + markdown fallback, bounded ([#10614](https://github.com/phoebe-health/phoebe/pull/10614) merged 1049b2646f)
- **admin-agent** — PHO-12306 leg B GitHub webhook ingestion — endpoint, HMAC fail-closed, event persistence + dedupe ([#10622](https://github.com/phoebe-health/phoebe/pull/10622) merged dc5d04d5c0)
- **admin-agent** — PHO-13042 read-only GitHub repo primitives + deploy doctrine skill; fixed missing actions:read App permission in review→fix loop ([#10552](https://github.com/phoebe-health/phoebe/pull/10552) merged 0aad4a0378)
- **admin-agent** — PHO-12980 core accounts API — owner filter, pagination, projection, evidence opt-in ([#10611](https://github.com/phoebe-health/phoebe/pull/10611) merged f9f1587f24)
- **cleanup** — PHO-12658, PHO-10748, PHO-11305, PHO-11344, PHO-10498 closed by Henry (completed in Linear, pruned from todo)
- **tools** — codex skills cleanse — 14 stock samples + orphans removed; wiki-vault/handoff/codex-goal-loop ported to Codex
- **tools** — vault system stood up — conventions, map, families, templates, todo, git+remote backup

## 2026-07-05

- **admin-agent** — PHO-12640 core-accounts read access closed as superseded by PHO-12921 (archived in Linear)

## 2026-07-03

- **phoebe** — PHO-12949 + PHO-12962 expected-failure alerting (monitors 288625692 / 302175306 live)
- **phoebe** — PHO-12955 `:eyes:` trace diagnosis
- **phoebe** — PHO-12952 report_missing_tool
- **phoebe** — PHO-12951 tool telemetry dashboard
- **phoebe** — PHO-12939 external tool specs / identifier resolution ([#10474](https://github.com/phoebe-health/phoebe/pull/10474))
- **phoebe** — PHO-12938 tool-output tiering
- **phoebe** — PHO-12931 Linear-style trace filters
- **phoebe** — eval harness fidelity + runner robustness — scheduler pass-budget fix, tree-scoped claims, answer-key leak removal ([#10480](https://github.com/phoebe-health/phoebe/pull/10480))

## 2026-07-01

- **admin-agent** — PHO-12830 Linear default project-scope resolution fix (Linear: Merged)
- **admin-agent** — PHO-12826 codebase tools no longer starve run lease heartbeats (Linear: Merged)
