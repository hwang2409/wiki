---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-08-19
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **WIKI — orchestrator `wiki-dev` (post-restart session, 2026-08-19).** Merged today: WIKI-340 (#275 font parity), WIKI-345 (#277 wk engine extraction), WIKI-346 (#278 wk render.py + authoritative normalizer enumeration, 10 review rounds), **WIKI-339 (#274 per-run event store sharding, 10 rounds — kills the provider-persistence-failed class)**. Live lanes: WIKI-352 (PR #279 persistent event-store connections; round-4 review MERGE-READY; now rebasing onto #274's per-shard connections, then confirmation review at rebased head); WIKI-361-PLAN (cdx luna xhigh, designing "wk owns the claude/codex agent loops", deliverable /tmp/wiki-361-plan.md -> vault).
- **wk TUI ladder REPRIORITIZED (Henry 2026-08-19 ~15:30): WIKI-361 architectural refactor first.** WIKI-347 (TUI shell) spawned then parked/abandoned 15 min in — do not respawn until the WIKI-361 plan decides what survives. Ladder 348-351 gated behind 361. Design: [[wk-tui-design]].
- **Supervisor bugs ticketed from today's incidents:** WIKI-353 (registry wipe on backend restart), WIKI-354 (deterministic run-id archive collision), WIKI-355 (uncontrolled recovery provider, /stop refuses), WIKI-356 DONE (timeout diagnosis: single-loop reactor + sync persistence/archive — [[supervisor-timeout-diagnosis-2026-08-19]]), WIKI-357/358/359 (P1 loop-offload fixes), WIKI-360 (recovery race noise). #279 is throughput only — 357/358 are the real timeout fixes.
- Queue after current lanes: WIKI-341/342 (font parity layers 2-3, unblocked), WIKI-343 (P3), WIKI-357-360.
- Henry switched claude accounts 2026-08-19 ~15:35 — no session limit concerns.
- **NEWT (orch `tooling-dev`) — NEWT-33 analytic derivatives MERGED (PR #78, 985454f) 2026-08-19 ~19:45Z after 10 review rounds.** DECISION (Henry 2026-08-19): GitHub Actions REMOVED from tooling repo (workflows deleted 28a77b9, Actions disabled repo-level) — he won't pay for Actions; the tooling merge gate is now review + local validation (cargo test, alloc_guard, clippy -D warnings, fmt, libm-free grep). Queue: SDF, mesh-mesh, native-CCD alignment. GOTCHA: branch newt-convex-ccd + PR #76 = prior NEWT-32 WIP, don't touch without Henry.

## Watchouts

- **GitHub Actions billing broken on hwang2409 account (2026-08-19): private-repo CI jobs die at scheduling (0 steps, runner_id=0, "payments failed" annotation).** Tooling repo is now CI-free by decision; wiki repo still has Actions — check the annotation before blaming runners. Henry fixes in Settings -> Billing & plans if/when he wants Actions back.
- **Supervisor write calls (archive/steer/spawn) intermittently time out under fleet load but usually COMPLETE server-side.** Verify via reads (`wiki agent status <t>`; CLI needs full path /Users/henry/me/fun/wiki/wiki) before retrying; retries reuse request_id (idempotent). Diagnosed in WIKI-356; fixes ticketed.
- **Archive-on-read: wait for runtime_state=idle before archiving a reviewer** — archiving mid-final-turn loses the findings body (protocol note updated 2026-08-19; WIKI-352-REVIEW2 lost this way).
- **Registry repair playbooks** (wipe reseed w/ correct entry schema — `orch` not `orchestrator_id`; run-id collision re-id of OLD archive artifacts, never the live run; stranded runs) in [[orchestrator-worker-protocol]] 2026-08-19 entries.
- Known pre-existing main red: test_agent_runtime_store.py test_codex_failed_render_completion_preserves_write_time_event.
- Full-suite SIGKILL-at-71% on the old wiki-339 branch was unbounded artifact-index memory — fixed in #274. Exit-137 with no summary line = suspect memory, run with RSS capture.
- Codex: credits balance 0 but pro-plan window healthy (16% used) — cdx luna/sol workers run fine.
- PHOEBE/NEWT: owned by phoebe-dev / tooling-dev orchestrators this session; wiki-dev does not touch them.
- Merge authority: wiki = orchestrator merges after clean review pass. Phoebe: never.
