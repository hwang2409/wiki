---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-15
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-15 eve: code-editor arc WAVE 2 COMPLETE, wiki fleet EMPTY** — WIKI-121 CM6 editor (#95, 6 review rounds), WIKI-122 explorer polish (#96, 4 rounds), WIKI-125 note rendering: mermaid/images/SVG/tables + vault asset endpoint (#97, 4 rounds) all merged; main at 76e8726. Native bundle rebuilt post-merge — **Henry must relaunch Wiki.app** to load new sidecar (125's asset endpoint is backend). 121 lesson: interactive Suspense-fallback handoff spawned 4 rounds of race bugs; orchestrator ordered design simplification (non-interactive fallback, delete handoff machinery) → clean next round. Simplify > patch when the same mechanism bugs 3+ rounds.
- **Reviewer lifecycle doctrine (Henry 2026-07-15, in protocol note)**: one sol reviewer per round — archive `closed` immediately after verdict routed, spawn fresh `<TICKET>-REVIEW<n>` pinned at new head SHA next round. At wrap-up stop BOTH monitors (worker + reviewer) — reviewer monitor got orphaned twice this arc.
- **Open design threads (Henry engaged, not yet approved/ticketed)**: (1) multi-workspace file browsing — clarified: orchestrator-derived workspace list, global sidebar switcher, vault always visible; approach A (workspace-id param on file API, registry-derived allowlist) proposed, AWAITING design approval → then design doc + tickets (127+); (2) code-viewer density fix (~1.4 line-height, prose styles leak into shiki lines) — approved direction, unticketed; (3) daemon-ize backend (launchd, app becomes client — kills relaunch-fleet-wipe) — Henry interested, no ticket yet; (4) WIKI-126 surface rebrand filed, BLOCKED on Henry picking a name (internals stay `wiki`).
- **Open wiki tickets**: WIKI-112 (dead-archive playwright flake — bit every gate this arc, reviewers rerun past it), WIKI-113, WIKI-116/117/118, WIKI-106/107, WIKI-124 (svg size threshold — may be fixed by #97 asset work, verify before starting).
- **Turbopuffer verdict**: anti-fit for local single-tenant wiki; real gap = semantic search → local sqlite-vec + embeddings if pursued. In [[turbopuffer]].
- **Phoebe**: PHO-13763 worker was dead (app-server exit) pre-relaunch; phoebe orch owns it. PHO-13800 todo line added by another orch.
- **`.codex/worktrees/` ~50 stale entries** — prune pending Henry (this arc's 10 review/impl worktrees all cleaned).

## Recent facts

- Reviewer status-file `state` can read merge-ready while step text says NOT-MERGE-READY — verdict text authoritative.
- Monitor glob `*dead*` false-fires on "dead-archive" flake text — match specific runtime states only.
- Backend serves frontend live from `frontend/dist` (rebuild → reload), backend code needs `make native-build` + relaunch.
- WIKI-117 env caveat: independent reruns `env -u WIKI_AGENT_ROLE`; pytest via `.venv/bin/python`.
- Local uncommitted package-lock churn blocks `git pull` after merges — checkout generated files, upstream wins.

## Watchouts

- Check todo.md ID collisions before filing (next free: WIKI-127).
- Pin gate verification + review worktrees to the SHA under review.
- Stale pre-steer merge-ready status: require fresh step text before re-gating.
- Ship-shaped findings → ticket IMMEDIATELY.
