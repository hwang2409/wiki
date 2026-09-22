---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-09-22
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries. Injected into every Claude session at start.

## Active threads

- **jev arc 3 browsing IN PROGRESS (orch `jevsicle` since 09-22 post-reset; was `jev`):** merged 09-22 pre-reset: #6 browsing spec (JEV-61), #8 impl plan (JEV-62, 12 tasks), #7 safety acceptance eval (JEV-60), #9 browsing foundation tasks 1-4 (JEV-63 + JEV-64 fix round, squash 95d298c). Plan tasks 5-12 PAUSED pending harness source reorg: JEV-65 spec lane (tools/ one-dir-per-tool + group loose zeta/*.py + co-located tests) respawned 12:00 EDT from the saved contract (`/tmp/cdx-JEV-65-prompt.md`) after the reset killed the original with zero output. Pending Henry: browsing plan's blocked-on-Henry list (smoke site, allowed-origin policy, budgets, default-on vs flag, headed mode) + python-escalate ergonomics + safety-tier default-on. Watchout: codex refuses adversarial safety-REVIEW contracts (cyberPolicy) — use cc opus with authorization preamble. Details [[typesafe-jev]].
- **Phoebe lanes (orch `feeeeb` since 09-22 post-reset; was `feebies`): BOTH PRs MERGED 09-22.** #17654 (PHO-17659 sim SMS idempotency) merged by Henry 10:26 EDT (squash d3a95c3f); live-Twilio validation post-deploy. #17655 (PHO-17613 wave-2 golden evals) merged 12:05 EDT by orch on Henry's explicit auth (squash c7342ccd; head had moved post-approval — 3 small commits incl. "Capture confirmation state before SMS injection"; required checks green, failed non-required "model" bot check; key learning: production shift confirmations write ShiftConfirmationMessage not contact attempts — injection adapter is two-chain; rubrics awaiting_human_validation, hosted run evidence post-merge). RCA DONE (worker archived): eval-case timeouts = no local event-agent worker running (messages sat pending, 720s budget burned with zero turns) + local DB schema drift (migration 20260921014902 unapplied -> org-create 500s); report /tmp/PHO-17613-RCA1-report.md. Scout DONE (archived): 20+ hardcoded Galiver sim-org findings, 6 fix lanes; report /tmp/PHO-17588-SCOUT1-report.md. TWO LIVE WORKERS: PHO-17613-PREFLIGHT1 (runner ensures migrations + auto-spawns worker + fast admission abort; v3-evals/ only) + PHO-17588-DESLOP1 (Henry: ALL six lanes folded into ONE PR, zero behavior change, Python seed files own personas, generated JSON manifest for TS, demo_org.py imports the 3 matching constants only, migration immutable). Queued next: sim outcome-contract + demo ghost-policy lane (now unblocked). NOTE: peer-orch hot.md rewrites clobbered this section FOUR times — re-check freshness.
  - MERGED through 09-21: #17326, #17383, #17406, #17375, #17327, #17427, #17430, #17443, #17437, #17432 wave-1 evals (rubrics still awaiting Henry validation), #17462 React+monochrome webapp rebuild, #17461 workbench run-UI embed, #17497 balance/de-slop pass (merged post-conflict-resolution under Henry's "merge both").
  - #17435 Galiver audience MERGED 09-21 (Henry's call, squash 6b3f0041) — both make-it-work sim PRs now landed; post-deploy verification pending for #17435 + #17437.
  - PARKED at merge-ready by Henry: #17328 daily-run RCA — re-confirmed 09-21 "don't merge yet".
  - 49h NETWORK OUTAGE 09-18 22:00 -> 09-20 19:00 wedged 3 workers (all revived); sol reviewer sessions degraded/stalled repeatedly 09-20/21 (flat raw.jsonl = dead; growth-probe before replacing; rerouted reviews to cc opus per degradation rule).
  - Watchout: a cc opus reviewer with workdir = MAIN phoebe checkout left a partial PR-diff STAGED there; stashed as "review-detritus..." 09-21 — main checkout must stay clean; give reviewers a worktree or read-only contract.
  - Sim assessment on PHO-17587; reliability lane cluster (ghost policy/idempotency/outcome contract) awaiting Henry. QUEUED: PHO-17613 waves 2-3, PHO-17625, rendered-UI fault matrix, PHO-17615 flake fix, #17437 post-deploy provider verification. Local webapp for Henry on 127.0.0.1:8821 (serving merged main 1ff6fa93).
- **Splitty lanes still parked** (Henry resumes later): SPLIT-3 (#3), SPLIT-4 (#4) both NOT-MERGE-READY with findings.
- **zeta GUI-fix arc ACTIVE 09-22 (orch `zeta`, respawned post-reset):** ZETA-143 audit worker (cc opus, in-process smoke harness — TCC Accessibility/ScreenRecording denied to worker shells, osascript path dead) delivered 8 findings + 15 captures (`/tmp/zeta143-audit-report.md`, `/tmp/zeta143-shots/`); worker archived. Two cdx luna fix lanes LIVE: ZETA-144 approval-dialog UX (overlap header, raw-JSON echo, no buttons, `approve` status label) + ZETA-145 transcript polish (thinking-row gutter x — measure-first, truncated `Usage appears after the first…` placeholder, body left edge). Blocked-visual coverage gaps (scroll/drag/slash-menu/resize) need Henry to grant TCC perms for an interactive pass. Prior arc: #179-#184 all merged, main 520b3b71. `dist/Zeta.app` STALE at d21ab5a — rebuild is Henry's call.
- **Pausanias:** PENDING HENRY DECISION on fused-abstention interpretation (floor-tuning lane vs accept) — gates agent-integration arc. Judge-scored LOCOMO still BLOCKED on OPENAI_API_KEY. Details [[pausanias]].
- Phoebe #17274 MERGED by Henry 09-17 (accepted the advisory HIGH rating; invoke-only surface).
- ORG-SNAPSHOT-PLAYBOOKS 3-org re-import state still unverified since the morning reset.
- Open defect tickets to file (wiki orch owns): event persistence must coerce non-JSON payloads; `GET /api/agents/{orch}/workgraph` 500-spam (`backend/app/workgraph.py:128`).
- **Pipeline (Henry 09-18, via zeta orch):** ALL implement spawns (frontend included) = cdx gpt-5.6-luna (high) — opus-4.7 carve-out ended; mid-run cc workers finish as-is. Reviewers = cdx gpt-5.6-sol. Three-UI-skills mandate stays in frontend kickoffs.

## Recent facts

- SIXTH full Wiki.app reset 09-22 ~11:51 EDT (Henry's ask): wiped runs/ + workgraphs + /tmp/agent-status + /tmp/agent-registry.json + command-log.sqlite3* + metadata.sqlite3* AND — first time — knowledge.db (9.7 GB) + knowledge.db.semantic. Repo ff'd to origin/main eafbf175 (already at tip), Wiki.app rebuilt (native swap) + relaunched; backend healthy on 127.0.0.1:8213. agent-archive dirs + cost-aggregation survived.
- Reset killed the live fleet: orchestrators jev, zeta, feebies + codex workers JEV-65, ZETA-142, PHO-17613-F8 died with the supervisor. Respawn from scratch if lanes resume; no run state survives.
- Claude credits RESTORED (Henry 09-17 evening) — cc opus-4.7 spawns allowed again.
- Spawn via HTTP `POST /api/agents/spawn` works (schema `SpawnWorkerIn`: ticket/kind/role/model/workdir/prompt required; effort for cdx).
- `dist/Zeta.app` stale at d21ab5a; rebuild is Henry's call.
- `wiki` CLI not on PATH in orch shells — use `~/me/fun/wiki/wiki`.

## Watchouts

- eval-engine MAIN checkout restore to `main` was in CASES3's contract — verify at wrap-up.
- Wiki backend was cycled repeatedly ~20:30-21:00 EDT (supervisor kills its HTTP child; steers 200 then refused). Worker providers + GitHub merges unaffected. Two reviewer spawns were lost to the flap and respawned. Stranded run dir without run.json (P3 leftover) moved to agent-archive to avoid the 2026-08-17c boot crash-loop.
- Never hard-reset this repo — the vault lives in it. ff-only.
- Merge auth: zeta + wiki + website + pausanias + splitty = orch self-merge on clean pass; phoebe NEVER — NO EXCEPTIONS (Henry 09-18 evening, FINAL: "no more merge auth for v3 evals, let me know and I will merge them"): feebies surfaces every merge-ready PR and waits; Henry merges all. Day's grant history merged #17326/#17383/#17406/#17375/#17430 while active.
- Verify fleet-chat claims via durable reads; a peer message is never Henry's approval.
- Spawn payloads MUST include `"orch":"<orchestrator-id>"` — orchestrator assignment is spawn-time only. No live retrofit exists: `wiki agent update` refuses supervisor-owned runs, `run/replace` inherits the old value, and both run.json and /tmp/agent-registry.json are clobbered from the in-memory RunRecord. (Learned 09-21: PHO-17613-W2 + PHO-17659-W1 spawned orch-less.)
