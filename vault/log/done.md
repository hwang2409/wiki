---
type: log
tags: [log, done]
created: 2026-07-06
updated: 2026-07-09
---

# Done

## 2026-07-09

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
