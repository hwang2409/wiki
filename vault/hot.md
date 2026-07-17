---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-17
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-17: MITMWEB WAVE 1 COMPLETE** (misc orch) — B2 capture/store merged (main@712df98, candidate 9a1d1dc, 15 review rounds): adapter/sequencer/gate/sink, atomic ownership handoffs, durable drains, bounded retention, released-flow accounting guard. Full suite+ruff+mypy-strict green on merged main. S0/B1/F3/F1 merged earlier. Campaign note: [[mitmweb-rebuild]]. **Wave 2 launching: B3 API + F2 flow grid** (cc workers — codex quota dead). Then I1 integration → {P1 packaging, F4 restyle}. F4 (Henry 2026-07-17): restyle web UI to wiki-app theme/layout, fable worker, spawn after I1 merges.
- **CODEX QUOTA EXHAUSTED until Jul 23 ~10:14 ET** — all cdx spawns fail (usageLimitExceeded). Entire fleet runs cc/claude-fable-5 until reset. Prior misc orch + B2 worker both died on this; replaced with cc.
- **Misc clone arc** (2026-07-16): pufferclone v1+v2 (PUF-8..12, main@6bc5d64), `tix` (TIX-1), `gauge` TSDB v0 (GAU-1..5, master@9d32f0b). Next clone candidates in [[local-cloud]]: Temporal-lite vs LSM KV — Henry pending. Unticketed: cargo install tix/puf/gauge, tix vault migration, PUF-13 NVMe tier.
- **Wiki**: wave 4b done (WIKI-117+118 #103@8f68d53). Sidecar rebuild debt: #103 backend changes not in deployed sidecar — fold into next swap (staged flow: FORCE_STAGE_ONLY=1, SIGTERM supervisor, swap-native-app.sh, relaunch). Open: daemon-ize backend (unticketed, Henry interested), WIKI-126 rebrand blocked on name, compact-mermaid-preview unfiled, WIKI-92/109/94 flake candidates. Next free ID: WIKI-131.
- **Phoebe** (phoebe orch): admin security doctrine complete (PHO-13950 #11594 + PHO-13957 #11637 merged — sandbox-only agent code). Live: PHO-12880 #11548 (CI blocked by GH incident), PHO-13944-PR2 #11630 (mergeable, awaiting Henry), PHO-13979, PHO-13980 #11656. Parked drafts: #11383 scratchpad (resume before merge-driving), #11475 tpuf scoping (awaiting Henry). Roadmap: PHO-13940 org context graph next big arc. Flakes: PHO-13860.

## Recent facts

- mitm-inspector requires Python 3.12 — sequencer intentionally rejects 3.13; `uv sync --python 3.12` in every new worktree (uv defaults to 3.13).
- mitm merge flow: local repo, no remote/PR — merge --no-ff into main, gates on merged main, remove worktrees, delete branch.
- Reviewer state field unreliable — verdict step text authoritative, always.
- Steer mode default `now` (Henry); on-idle only for genuinely-next-task input.
- Fleet monitor doctrine (Henry): watchlist file + re-alarms (verdict q5m, review-gap q10m, staleness 30m); never rebuild monitors per spawn/archive.
- `wiki` CLI not on monitor-shell PATH — use `~/me/fun/wiki/wiki` absolute.
- Supervisor MCP ops can 1s-timeout under load — verify effect before retrying steers. Soft cap 5 workers; above it expect provider timeouts.
- Workers survive backend restarts; status file `/tmp/agent-status/<T>.json` readable when backend down.
- cdx-only gotchas (dormant until quota reset): plugin-install elicitation hangs turns — ban in spawn prompts; cyberPolicy kills "adversarial/attack"-phrased review prompts — phrase "correctness/robustness"; startup hangs — replace_agent; after token re-auth replace every live cdx worker.
- Phoebe: BuildBuddy ExecuteWorkflow reruns check out raw branch (no main merge) — for fixes-on-main, merge origin/main + push. Bazel cache can hide time-bomb tests — unchanged-target red → suspect main. Worker test policy (Henry): no full local suites, directly-touched targets only, CI authoritative.

## Watchouts

- Next free ticket ID: WIKI-132 (131 used 2026-07-17: chat table scroll fix, e5370de).
- Pin review worktrees + gates to exact SHA; archive reviewer immediately after verdict routed.
- Workers' claims verified by mutation — keep ordering it in review prompts.
- Local main can hold unpushed vault commits — pull --rebase before push.
- `.codex/worktrees/` ~40 stale entries — prune pending Henry.
