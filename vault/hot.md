---
type: reference
tags: [hot]
created: 2026-07-06
updated: 2026-07-17
---

# Hot Context

Rolling ≤500-word session cache. Rewrite (don't append) at work-arc boundaries: what changed, active threads, watchouts. Injected into every Claude session at start.

## Active threads

- **2026-07-17: MITMWEB I1+F4+F5+F6 MERGED, LIVE-ITERATE MODE, P1 PACKAGING QUEUED** (misc orch) — I1 integration @ 8e73838 (3 rounds), F4 restyle @ e0f474b (4 rounds), F5 minimalist b/w @ main@ba80c53 (candidate 874280f, 3 rounds cc/opus-4.7): light-default b/w palette w/ explicit allowlist covering tokens.css AND index.html, JSON auto-parse tree, SSE frame streaming (retains truncated suffix), /v1/messages summary w/ always-both-slots fallback (`<method> <path> · <status_or_—> · <duration_or_—>`), useSeenFlows hook (retained-ID pruning survives reconnect + rollover), response_status 100–599 range across schema/py/ts. Merged main: 388 pytest, ruff, mypy pkg-scope, tsc, eslint, **196 vitest** + 4-width Playwright e2e on py3.12. Live proxy runnable via `make up` (backend :8000 + reverse proxy :8080 → api.anthropic.com + vite dev :5173) — Makefile written 2026-07-17, uncommitted on main. Henry ran it against Anthropic. Campaign [[mitmweb-rebuild]]. Then F6 density+right-panel @ main@f3ab43a (candidate be11314, cc/opus-4.7, NO review round — Henry chose live-iterate mode for design polish tickets): right-side inspector dock replacing bottom, killed six layers of chrome around body panel (REQUEST BODY / Bounded capture / CAPTURED / TOTAL/CAPTURED/TYPE / explainer footer), uncapped frontend decoder (Math.min clamp gone, DEFAULT_BODY_LIMIT lifted to wire ceiling ~3 MiB), raised backend `--max-body-prefix-bytes` default to wire ceiling, JSON tree default view for detected JSON, largeBody.dom.test.tsx (+104) pins main-thread budget on 2 MiB fake body, contracts/limits.ts + limits.contract.test.ts (+82) keeps schema/backend/frontend in sync. Merged main: 550 pytest, ruff, mypy, tsc, eslint, **205 vitest** + 4-width Playwright. **Only P1 packaging remains** — Henry cue.
- **CODEX BACK 2026-07-17**: Henry re-authed with a DIFFERENT account — fresh quota, cdx spawns work (probe-verified). Default pipeline restored (luna implement / sol review). Old-account gotchas still apply after any future re-auth (replace live cdx workers so processes hold fresh tokens).
- **CLAUDE-FABLE-5 OFF-PLAN 2026-07-17**: fable-5 no longer covered on Henry's plan — every spawn blocks "Usage credits are required for this model." Requires paid credits, never use as default. Default cc model = claude opus-4.7 (or route implement work through cdx/luna). Only pick fable-5 if Henry explicitly asks and accepts credit charge. Applies to future workers too (e.g. F4 restyle).
- **Misc clone arc** (2026-07-16): pufferclone v1+v2 (PUF-8..12, main@6bc5d64), `tix` (TIX-1), `gauge` TSDB v0 (GAU-1..5, master@9d32f0b). Next clone candidates in [[local-cloud]]: Temporal-lite vs LSM KV — Henry pending. Unticketed: cargo install tix/puf/gauge, tix vault migration, PUF-13 NVMe tier.
- **Wiki 2026-07-17 double merge**: WIKI-132 (ticket/PR dashboard, #105@6c52a60 squash, 5 rounds) + WIKI-133 (j/k scroll, #104@643628b squash, 3 rounds). 132 had rebase-on-main mid-loop after 133 landed. Dashboard = `/api/dashboard/tickets` merging supervisor registry + status files + archives + phoebe-allowlist gh-PR-cache w/ prod-deploy compare; 15s poll; component-integration tests via @testing-library/react (dev dep added). Sidecar rebuild debt: #103 backend changes not in deployed sidecar; NOW also #105 backend + frontend not deployed — fold into next swap (staged flow: FORCE_STAGE_ONLY=1, SIGTERM supervisor, swap-native-app.sh, relaunch). Open: daemon-ize backend, WIKI-126 rebrand blocked on name, compact-mermaid-preview unfiled, WIKI-92/109/94 flake candidates.
- **Phoebe** (phoebe orch): admin security doctrine complete (PHO-13950 #11594 + PHO-13957 #11637 merged — sandbox-only agent code). PHO-13944-PR2 #11630 merged at 0e40d33efc; classifier edge cases deferred to PHO-14034. Live: PHO-12880 #11548 (CI blocked by GH incident), PHO-13979, PHO-13980 #11656. Parked drafts: #11383 scratchpad (resume before merge-driving), #11475 tpuf scoping (awaiting Henry). Roadmap: PHO-13940 org context graph next big arc. Flakes: PHO-13860.

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

- Next free ticket ID: WIKI-134 (131 = chat scroll e5370de, 132 = dashboard #105@6c52a60, 133 = j/k scroll #104@643628b).
- Mastermind merge-ready loop cap raised 3→8 (see [[feedback_merge_ready_iteration_cap]]) — gpt-5.6-luna needs more rounds; auto-steer unless plateau on same finding for 3 straight rounds.
- Pin review worktrees + gates to exact SHA; archive reviewer immediately after verdict routed.
- Workers' claims verified by mutation — keep ordering it in review prompts.
- Local main can hold unpushed vault commits — pull --rebase before push.
- `.codex/worktrees/` ~40 stale entries — prune pending Henry.
- CI polling: use `gh_pr_watch.py --watch` (babysit-pr script), NEVER bash `until [ "$(gh pr checks ... | awk ...)" = "pass," ]` — awk/sort predicate silently misses pending→green (bit us on PHO-14003 #11688 2026-07-17).
- Steer cap: `steer_agent.text` max_length = 4000 chars — long multi-finding steers hit ValidationError. Write full detail to `/tmp/<ticket>-review<n>-findings.md`, send terse steer pointing at file path (bit us on MITMWEB-F5 REVIEW1 2026-07-17).
